"""
Optional real speaking-time tracking.

Mic-unmuted time (tracked in voice_tracking.py) is NOT the same as actually
talking - someone can sit unmuted in silence for hours. To measure real
speaking time we have to look at actual voice activity, and the regular
discord.py gateway simply doesn't expose that. The only way to get it is for
the bot to join the voice channel itself and read the voice-activity signal
Discord sends over the voice websocket (the same signal that lights up the
green ring around someone's avatar).

We use the third-party `discord-ext-voice-recv` extension for this. It gives
us synthesized `on_voice_member_speaking_start` / `_stop` events without us
having to implement RTP/voice-gateway parsing ourselves. Audio is received as
opus packets and immediately discarded in `write()` - it is never decoded,
buffered, written to disk, or inspected in any way. Only the boolean
speaking-state transitions (and their timestamps) are kept, and those are
handed straight to `VoiceTracking.set_speaking_threadsafe`, which is the same
place voice/mute/stream time is accounted, persisted, and flushed.

IMPORTANT LIMITATIONS - please read before enabling this
----------------------------------------------------------------------------
1. A bot can only be connected to ONE voice channel per guild at a time.
   That's a hard Discord limitation, not a bug here. If a guild has multiple
   active voice channels simultaneously, this cog will only be able to
   measure real speaking time in whichever single channel currently has the
   most people (see `_best_channel` below); other channels still get
   accurate voice/mute/stream time from voice_tracking.py, just not
   `speaking_seconds` while this cog is elsewhere.
2. This requires the bot to actually join voice (uses the "Connect"
   permission, still nowhere near Administrator) and pull in extra native
   dependencies (PyNaCl, libopus, ffmpeg) plus the experimental
   `discord-ext-voice-recv` package, which the discord.py project itself
   does not officially support. Treat it as a "best effort" feature.
3. Depending on your jurisdiction and community norms, a bot silently
   joining a voice channel to measure "who is talking" can be a privacy
   concern even though no audio is ever stored - tell your members it's
   happening.

This whole cog is a no-op unless ENABLE_SPEAKING_TRACKING=true is set, and it
simply won't load if the optional dependency isn't installed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands, tasks

from cogs.voice_tracking import VoiceTracking

log = logging.getLogger(__name__)

try:
    from discord.ext import voice_recv

    VOICE_RECV_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when dep is missing
    VOICE_RECV_AVAILABLE = False


if VOICE_RECV_AVAILABLE:

    class SpeakingSink(voice_recv.AudioSink):
        """Turns received audio into speaking-state events and nothing else.

        `write()` deliberately discards every packet immediately - no audio
        is ever stored, decoded to a file, or kept in memory beyond the
        instant it takes to drop it.
        """

        def __init__(self, voice_tracking: VoiceTracking):
            super().__init__()
            self.voice_tracking = voice_tracking

        def wants_opus(self) -> bool:
            # We never inspect the audio itself, only the speaking-state
            # events synthesized from packet timing, so ask for raw opus
            # (cheaper - no PCM decode) rather than decoded audio.
            return True

        def write(self, user, data) -> None:
            return  # intentionally discard all audio data

        def cleanup(self) -> None:
            return

        # These are sync callbacks dispatched from a background thread
        # (per discord-ext-voice-recv's design), so they must hand off to
        # the bot's event loop via the threadsafe wrapper rather than
        # touching asyncio state directly.
        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_start(self, member: discord.Member) -> None:
            self.voice_tracking.set_speaking_threadsafe(member.guild.id, member.id, True)

        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_stop(self, member: discord.Member) -> None:
            self.voice_tracking.set_speaking_threadsafe(member.guild.id, member.id, False)


class SpeakingTracking(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        voice_tracking: VoiceTracking,
        *,
        min_members: int,
        idle_timeout: int,
    ):
        if not VOICE_RECV_AVAILABLE:
            raise RuntimeError(
                "discord-ext-voice-recv is not installed. Install it with "
                "`pip install discord-ext-voice-recv` or disable "
                "ENABLE_SPEAKING_TRACKING."
            )
        self.bot = bot
        self.voice_tracking = voice_tracking
        self.min_members = min_members
        self.idle_timeout = idle_timeout

        self.voice_clients: dict[int, "voice_recv.VoiceRecvClient"] = {}
        self.idle_tasks: dict[int, asyncio.Task] = {}

    def cog_unload(self) -> None:
        self._reconcile_loop.cancel()
        for task in self.idle_tasks.values():
            task.cancel()
        for vc in list(self.voice_clients.values()):
            self.bot.loop.create_task(vc.disconnect(force=True))

    async def cog_load(self) -> None:
        self._reconcile_loop.start()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._reconcile_guild(guild)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        await self._reconcile_guild(member.guild)

    # Safety-net loop in case a connection drops without a voice_state_update
    # for a human member to trigger reconciliation (e.g. a network blip).
    @tasks.loop(seconds=60)
    async def _reconcile_loop(self) -> None:
        for guild in self.bot.guilds:
            await self._reconcile_guild(guild)

    @_reconcile_loop.before_loop
    async def _before_reconcile_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #

    def _best_channel(self, guild: discord.Guild) -> Optional[discord.VoiceChannel]:
        candidates: list[tuple[int, discord.VoiceChannel]] = []
        for channel in guild.voice_channels:
            if not self.voice_tracking.is_trackable_channel(channel):
                continue
            humans = sum(1 for m in channel.members if not m.bot)
            if humans >= self.min_members:
                candidates.append((humans, channel))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: (-pair[0], pair[1].id))
        return candidates[0][1]

    def _schedule_idle_disconnect(self, guild_id: int) -> None:
        if guild_id in self.idle_tasks:
            return

        async def _task() -> None:
            try:
                await asyncio.sleep(self.idle_timeout)
            except asyncio.CancelledError:
                return
            vc = self.voice_clients.pop(guild_id, None)
            self.idle_tasks.pop(guild_id, None)
            if vc is not None and vc.is_connected():
                await vc.disconnect(force=True)

        self.idle_tasks[guild_id] = self.bot.loop.create_task(_task())

    def _cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self.idle_tasks.pop(guild_id, None)
        if task is not None:
            task.cancel()

    async def _reconcile_guild(self, guild: discord.Guild) -> None:
        target = self._best_channel(guild)
        vc = self.voice_clients.get(guild.id)
        connected = vc is not None and vc.is_connected()

        if target is None:
            if connected:
                self._schedule_idle_disconnect(guild.id)
            return

        self._cancel_idle_disconnect(guild.id)

        if connected and vc.channel.id == target.id:
            return

        try:
            if connected:
                await vc.move_to(target)
            else:
                new_vc = await target.connect(cls=voice_recv.VoiceRecvClient, self_deaf=True)
                sink = SpeakingSink(self.voice_tracking)
                new_vc.listen(sink)
                self.voice_clients[guild.id] = new_vc
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            log.warning("Speaking-tracker couldn't join %s in %s: %s", target, guild, exc)