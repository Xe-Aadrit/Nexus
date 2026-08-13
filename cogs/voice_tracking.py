"""
Core voice-activity tracking.

Tracks, per (guild, user):
  * time spent connected to a (non-AFK, by default) voice channel
  * time spent with an effectively unmuted microphone
    (not self-muted AND not server-muted)
  * time spent screen-sharing ("streaming")

Speaking-time is *not* computed here (Discord's normal gateway gives us no
signal for that), but this cog exposes a small thread-safe API
(`set_speaking_threadsafe`) that the optional `speaking_tracking` cog uses to
feed real voice-activity data into the same per-user session, so all four
timers live in one place and get flushed together.

Design notes on correctness under restarts / crashes / concurrency
--------------------------------------------------------------------
- Every currently-open timer is represented purely in memory
  (`self.sessions`) as the wall-clock timestamp it was last (re)started at.
- A background task flushes *every* open session every
  `flush_interval_seconds`: it adds `now - started_at` to the persisted
  totals in the database and then rolls `started_at` forward to `now`. The
  same "flush the delta, then roll the clock forward" helper is used for
  ordinary state-change events (mute, unmute, move, disconnect, stream
  start/stop), so accounting is always exact between flush points and never
  double-counts.
- Because of that periodic flush, a hard crash (`kill -9`, power loss) can
  only ever lose up to `flush_interval_seconds` of data per active user -
  everything before that is already durably in `user_stats`.
- On a clean or unclean restart we do NOT try to resume old sessions across
  the downtime gap (we have no reliable way to know what actually happened
  to voice state while we were offline). Instead we discard any leftover
  session checkpoints and rebuild sessions fresh from whatever the gateway
  reports as the *current* voice state once we're back up. This is a
  deliberate, documented tradeoff: better to lose the (bounded) partial
  interval than to silently fabricate activity for a downtime window.
- All mutations to a given user's session go through an `asyncio.Lock`
  scoped to that (guild_id, user_id) pair, so overlapping events (e.g. a
  mute + a channel move delivered in the same gateway payload, or a
  concurrent periodic flush) can never interleave and corrupt state.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands, tasks

from database import Database, SessionRow

log = logging.getLogger(__name__)


@dataclass
class SessionState:
    channel_id: int
    voice_started_at: float
    unmuted_started_at: Optional[float] = None
    speaking_started_at: Optional[float] = None
    streaming_started_at: Optional[float] = None


def effective_unmuted(vs: discord.VoiceState) -> bool:
    """A user's mic is only actually usable if neither self-muted nor
    server-muted."""
    return not (vs.self_mute or vs.mute)


class VoiceTracking(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database, *, flush_interval: int, track_afk: bool):
        self.bot = bot
        self.db = db
        self.flush_interval = flush_interval
        self.track_afk = track_afk

        self.sessions: dict[tuple[int, int], SessionState] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ready_once = False

        self._flush_loop.change_interval(seconds=flush_interval)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        self._flush_loop.start()

    def cog_unload(self) -> None:
        self._flush_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready_once:
            return
        self._ready_once = True
        await self._recover_and_rebuild()

    async def _recover_and_rebuild(self) -> None:
        """Run once at startup: discard stale checkpoints, then reconstruct
        in-memory sessions from whatever the gateway currently reports."""
        async with self.db.lock:
            stale = await self.db.all_sessions()
            if stale:
                log.info(
                    "Discarding %d stale voice session checkpoint(s) from before "
                    "restart (bounded to <= %ds of data each).",
                    len(stale),
                    self.flush_interval,
                )
            await self.db.clear_all_sessions()

        now = time.time()
        rebuilt = 0
        for guild in self.bot.guilds:
            for channel in guild.voice_channels:
                if not self.is_trackable_channel(channel):
                    continue
                for member in channel.members:
                    if member.bot:
                        continue
                    vs = member.voice
                    if vs is None:
                        continue
                    key = (guild.id, member.id)
                    self.sessions[key] = SessionState(
                        channel_id=channel.id,
                        voice_started_at=now,
                        unmuted_started_at=now if effective_unmuted(vs) else None,
                        streaming_started_at=now if vs.self_stream else None,
                    )
                    await self._persist_checkpoint(guild.id, member.id)
                    rebuilt += 1
        if rebuilt:
            log.info("Rebuilt %d in-progress voice session(s) from live state.", rebuilt)

    async def shutdown(self) -> None:
        """Best-effort final flush, called from bot.py during graceful
        shutdown (SIGINT/SIGTERM or a clean `bot.close()`)."""
        self._flush_loop.cancel()
        keys = list(self.sessions.keys())
        now = time.time()
        for guild_id, user_id in keys:
            await self._flush(guild_id, user_id, now)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _lock(self, key: tuple[int, int]) -> asyncio.Lock:
        return self._locks[key]

    def is_trackable_channel(self, channel: Optional[discord.abc.Connectable]) -> bool:
        if channel is None:
            return False
        guild = getattr(channel, "guild", None)
        if not self.track_afk and guild is not None and guild.afk_channel is not None:
            if channel.id == guild.afk_channel.id:
                return False
        return True

    async def _persist_checkpoint(self, guild_id: int, user_id: int) -> None:
        state = self.sessions.get((guild_id, user_id))
        async with self.db.lock:
            if state is None:
                await self.db.delete_session(guild_id, user_id)
            else:
                await self.db.upsert_session(
                    SessionRow(
                        guild_id=guild_id,
                        user_id=user_id,
                        channel_id=state.channel_id,
                        voice_started_at=state.voice_started_at,
                        unmuted_started_at=state.unmuted_started_at,
                        speaking_started_at=state.speaking_started_at,
                        streaming_started_at=state.streaming_started_at,
                    )
                )

    async def _flush(
        self,
        guild_id: int,
        user_id: int,
        now: float,
        *,
        close_voice: bool = False,
        close_unmuted: bool = False,
        close_speaking: bool = False,
        close_streaming: bool = False,
    ) -> None:
        """Add elapsed time on every open timer to the persisted totals, then
        either roll each still-open timer forward to `now`, or clear it if it
        was requested to close. Must be called while holding the per-user
        lock."""
        key = (guild_id, user_id)
        state = self.sessions.get(key)
        if state is None:
            return

        voice_delta = max(0.0, now - state.voice_started_at)
        unmuted_delta = max(0.0, now - state.unmuted_started_at) if state.unmuted_started_at else 0.0
        speaking_delta = max(0.0, now - state.speaking_started_at) if state.speaking_started_at else 0.0
        streaming_delta = max(0.0, now - state.streaming_started_at) if state.streaming_started_at else 0.0

        async with self.db.lock:
            await self.db.add_deltas(
                guild_id,
                user_id,
                voice=voice_delta,
                unmuted=unmuted_delta,
                speaking=speaking_delta,
                streaming=streaming_delta,
            )

        if close_voice:
            del self.sessions[key]
            async with self.db.lock:
                await self.db.delete_session(guild_id, user_id)
            self._locks.pop(key, None)
            return

        state.voice_started_at = now
        state.unmuted_started_at = None if close_unmuted else (now if state.unmuted_started_at else None)
        state.speaking_started_at = None if close_speaking else (now if state.speaking_started_at else None)
        state.streaming_started_at = None if close_streaming else (now if state.streaming_started_at else None)
        await self._persist_checkpoint(guild_id, user_id)

    @tasks.loop(seconds=30)
    async def _flush_loop(self) -> None:
        now = time.time()
        for guild_id, user_id in list(self.sessions.keys()):
            async with self._lock((guild_id, user_id)):
                await self._flush(guild_id, user_id, now)

    @_flush_loop.before_loop
    async def _before_flush_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # event handling
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot:
            return

        guild_id = member.guild.id
        user_id = member.id
        key = (guild_id, user_id)
        now = time.time()

        before_trackable = self.is_trackable_channel(before.channel)
        after_trackable = self.is_trackable_channel(after.channel)

        async with self._lock(key):
            if not before_trackable and after_trackable:
                self.sessions[key] = SessionState(
                    channel_id=after.channel.id,
                    voice_started_at=now,
                    unmuted_started_at=now if effective_unmuted(after) else None,
                    streaming_started_at=now if after.self_stream else None,
                )
                await self._persist_checkpoint(guild_id, user_id)

            elif before_trackable and not after_trackable:
                await self._flush(
                    guild_id, user_id, now,
                    close_voice=True, close_unmuted=True,
                    close_speaking=True, close_streaming=True,
                )

            elif before_trackable and after_trackable:
                state = self.sessions.get(key)
                if state is None:
                    # Shouldn't normally happen, but stay defensive (e.g. if
                    # the cog was reloaded mid-session).
                    self.sessions[key] = SessionState(
                        channel_id=after.channel.id,
                        voice_started_at=now,
                        unmuted_started_at=now if effective_unmuted(after) else None,
                        streaming_started_at=now if after.self_stream else None,
                    )
                    await self._persist_checkpoint(guild_id, user_id)
                    return

                became_unmuted = effective_unmuted(after)
                was_unmuted = effective_unmuted(before)
                is_streaming = after.self_stream
                was_streaming = before.self_stream
                channel_changed = before.channel.id != after.channel.id

                if became_unmuted != was_unmuted or is_streaming != was_streaming or channel_changed:
                    await self._flush(
                        guild_id, user_id, now,
                        close_unmuted=not became_unmuted,
                        close_streaming=not is_streaming,
                    )
                    state = self.sessions.get(key)
                    if state is not None:
                        state.channel_id = after.channel.id
                        if became_unmuted and state.unmuted_started_at is None:
                            state.unmuted_started_at = now
                        if is_streaming and state.streaming_started_at is None:
                            state.streaming_started_at = now
                        await self._persist_checkpoint(guild_id, user_id)
            # else: not trackable before or after -> nothing to do

    # ------------------------------------------------------------------ #
    # public API used by the optional speaking-tracking cog
    # ------------------------------------------------------------------ #

    async def set_speaking(self, guild_id: int, user_id: int, is_speaking: bool) -> None:
        key = (guild_id, user_id)
        now = time.time()
        async with self._lock(key):
            state = self.sessions.get(key)
            if state is None:
                return  # user isn't in a tracked voice session (e.g. AFK channel)
            currently_speaking = state.speaking_started_at is not None
            if is_speaking and not currently_speaking:
                state.speaking_started_at = now
                await self._persist_checkpoint(guild_id, user_id)
            elif not is_speaking and currently_speaking:
                await self._flush(guild_id, user_id, now, close_speaking=True)

    def set_speaking_threadsafe(self, guild_id: int, user_id: int, is_speaking: bool) -> None:
        """Safe to call from a non-event-loop thread (the voice-recv sink
        callbacks run on their own thread)."""
        asyncio.run_coroutine_threadsafe(
            self.set_speaking(guild_id, user_id, is_speaking), self.bot.loop
        )

    # ------------------------------------------------------------------ #
    # read API for the stats command
    # ------------------------------------------------------------------ #

    async def live_seconds(self, guild_id: int, user_id: int) -> dict[str, float]:
        """Returns the *unflushed* extra seconds currently accruing for a
        user, so `!stats` can show up-to-the-second numbers without waiting
        for the next periodic flush."""
        key = (guild_id, user_id)
        state = self.sessions.get(key)
        if state is None:
            return {"voice": 0.0, "unmuted": 0.0, "speaking": 0.0, "streaming": 0.0}
        now = time.time()
        return {
            "voice": max(0.0, now - state.voice_started_at),
            "unmuted": max(0.0, now - state.unmuted_started_at) if state.unmuted_started_at else 0.0,
            "speaking": max(0.0, now - state.speaking_started_at) if state.speaking_started_at else 0.0,
            "streaming": max(0.0, now - state.streaming_started_at) if state.streaming_started_at else 0.0,
        }