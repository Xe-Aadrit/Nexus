"""
Anti Farm system.

Detects users who farm VC hours to game the VC Dominator / Streamer Of
The Week weekly roles by sitting in a voice channel while muted, deafened,
or completely alone for an extended period.

Definition of a "suspicious" VC state (any of the following):
    - User is server-deafened — they cannot hear anyone, clearly not
      participating.
    - User is self-deafened — same effective result.
    - User is the only human in the channel AND is muted (self or server) —
      nobody to talk to, and their mic is off.

If a user remains continuously in a suspicious state for longer than
ANTI_FARM_THRESHOLD_HOURS (default: 3 hours, configurable via .env), they
are flagged in the database. Flagged users are excluded from the VC
Dominator and Streamer Of The Week rankings during the next weekly reset.

Anti Farm flags are cleared at the start of each new week in weekly_roles.py.
The flag only affects the *weekly ranking*, not the all-time cumulative stats.

Toggle: !antifarm on|off  /  /antifarm on|off
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

from database import Database
from config import Config

log = logging.getLogger(__name__)


def _is_suspicious(member: discord.Member, vs: discord.VoiceState) -> bool:
    """Return True if this voice state looks like VC farming.

    Suspicious means the user cannot meaningfully participate:
      - They are deafened (server or self): they can't hear the channel, so
        they are just idling with the connection open.
      - They are the sole human in the channel AND are muted: there's nobody
        to talk to, and their mic is off.
    """
    # Deafened in any form → clearly not participating
    if vs.deaf or vs.self_deaf:
        return True

    # Alone in the channel AND muted → not interacting with anyone
    channel = vs.channel
    if channel is not None:
        human_count = sum(1 for m in channel.members if not m.bot)
        if human_count <= 1 and (vs.mute or vs.self_mute):
            return True

    return False


class AntiFarm(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database, config: Config):
        self.bot = bot
        self.db = db
        self.config = config

        # Per (guild_id, user_id): wall-clock timestamp (UTC epoch) when the
        # user entered a suspicious state. Cleared when they leave it.
        self._suspicious_since: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self._check_loop.start()

    def cog_unload(self) -> None:
        self._check_loop.cancel()

    # ------------------------------------------------------------------ #
    # Voice state tracking
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        if after.channel is None:
            # User left voice entirely — clear any pending suspicious timer
            self._suspicious_since.pop((member.guild.id, member.id), None)
            return

        # When a member's state changes, it may also affect whether others
        # in the same channel(s) are "alone". Re-evaluate all humans in
        # both the source and destination channels.
        channels_to_check: set[discord.VoiceChannel] = set()
        if before.channel is not None:
            channels_to_check.add(before.channel)
        if after.channel is not None:
            channels_to_check.add(after.channel)

        for channel in channels_to_check:
            for m in channel.members:
                if m.bot or m.voice is None:
                    continue
                self._update_suspicious(m, m.voice)

        # Also directly apply the moving member's new state (they may not
        # appear in after.channel.members if the cache hasn't settled yet)
        self._update_suspicious(member, after)

    def _update_suspicious(
        self, member: discord.Member, vs: discord.VoiceState
    ) -> None:
        key = (member.guild.id, member.id)
        if _is_suspicious(member, vs):
            # Only record the *start* of the suspicious period; don't reset
            # it on every call if the user is already tracked.
            if key not in self._suspicious_since:
                self._suspicious_since[key] = time.time()
        else:
            self._suspicious_since.pop(key, None)

    # ------------------------------------------------------------------ #
    # Periodic threshold check
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def _check_loop(self) -> None:
        """Flag any user whose suspicious-state duration has crossed the
        configured threshold, provided Anti Farm is enabled for their guild."""
        threshold = self.config.anti_farm_threshold_hours * 3600
        now = time.time()

        for (guild_id, user_id), started_at in list(self._suspicious_since.items()):
            elapsed = now - started_at
            if elapsed < threshold:
                continue

            async with self.db.lock:
                cfg = await self.db.get_guild_config(guild_id)

            if not cfg.anti_farm_enabled:
                continue

            async with self.db.lock:
                await self.db.flag_anti_farm(guild_id, user_id, started_at)

            guild = self.bot.get_guild(guild_id)
            member: Optional[discord.Member] = (
                guild.get_member(user_id) if guild else None
            )
            log.info(
                "Anti-farm: flagged %s (guild %d) after %.1fh in suspicious state.",
                member or user_id,
                guild_id,
                elapsed / 3600,
            )

    @_check_loop.before_loop
    async def _before_check(self) -> None:
        await self.bot.wait_until_ready()
