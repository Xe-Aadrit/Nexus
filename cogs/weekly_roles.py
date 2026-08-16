"""
Weekly automated role assignment.

Every Monday at 00:00 UTC, Nexus:
  1. Computes each member's activity delta for the week (current totals minus
     the snapshot taken at last Monday's reset).
  2. Excludes Anti-Farm-flagged users from the VC Dominator and Streamer Of
     The Week rankings (they may have farmed those stats).
  3. Identifies the top performer in each category:
       • Streamer Of The Week  — highest streaming_seconds this week
       • VC Dominator          — highest voice_seconds this week
       • Soul Of The Chat      — highest message_count this week
  4. Removes the role from the previous holder (if any), assigns it to the
     new winner. A user wins only if they have at least 1 second / 1 message.
  5. Optionally posts an announcement embed (if announcements are enabled and
     a channel is configured via !setannouncechannel).
  6. Takes a fresh weekly snapshot so next week starts from zero.
  7. Clears Anti Farm flags for the new week.

The check runs every minute so that the bot catches up on any missed reset
if it was offline during Monday midnight UTC.

Daily snapshots (for n-day activity queries) are taken every 30 minutes in
the same cog; the operation is idempotent per UTC day.
"""
from __future__ import annotations

import datetime
import logging
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

from database import Database
from config import Config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role metadata
# ---------------------------------------------------------------------------

_ROLE_KEYS = ("streamer", "vc", "chat")

_ROLE_NAMES: dict[str, str] = {
    "streamer": "Streamer Of The Week",
    "vc":       "VC Dominator",
    "chat":     "Soul Of The Chat",
}

_ROLE_ICONS: dict[str, str] = {
    "streamer": "🎥",
    "vc":       "🎙️",
    "chat":     "💬",
}


class WeeklyRoles(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database, config: Config):
        self.bot = bot
        self.db = db
        self.config = config

    async def cog_load(self) -> None:
        self._weekly_check.start()
        self._daily_snapshot_loop.start()

    def cog_unload(self) -> None:
        self._weekly_check.cancel()
        self._daily_snapshot_loop.cancel()

    # ------------------------------------------------------------------ #
    # Scheduled tasks
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=1)
    async def _weekly_check(self) -> None:
        """Fire the weekly reset for any guild that hasn't had one this week.

        "This week's Monday" is calculated fresh every minute in UTC, so the
        bot automatically catches up after downtime without needing its own
        state beyond what's in the database."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # Start of the current Monday (weekday 0) at 00:00:00 UTC
        days_since_monday = now_utc.weekday()  # 0 = Monday
        this_monday = (now_utc - datetime.timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        this_monday_ts = this_monday.timestamp()

        for guild in self.bot.guilds:
            async with self.db.lock:
                cfg = await self.db.get_guild_config(guild.id)

            # Only reset if we haven't already reset for this week
            if cfg.last_weekly_reset_ts < this_monday_ts:
                log.info(
                    "Running weekly reset for guild %d (%s).",
                    guild.id, guild.name,
                )
                await self._do_weekly_reset(guild, this_monday, this_monday_ts)

    @tasks.loop(minutes=30)
    async def _daily_snapshot_loop(self) -> None:
        """Take a daily snapshot for each guild (idempotent per UTC day).

        Runs every 30 minutes so that, even if the bot was briefly offline at
        midnight, the snapshot is taken within half an hour."""
        now_ts = time.time()
        for guild in self.bot.guilds:
            async with self.db.lock:
                await self.db.take_daily_snapshot(guild.id, now_ts)

    @_weekly_check.before_loop
    async def _before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    @_daily_snapshot_loop.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Weekly reset logic
    # ------------------------------------------------------------------ #

    async def _do_weekly_reset(
        self,
        guild: discord.Guild,
        week_start: datetime.datetime,
        week_start_ts: float,
    ) -> None:
        """Assign roles, announce winners (if configured), snapshot, and reset."""

        async with self.db.lock:
            deltas = await self.db.get_weekly_deltas(guild.id)
            flagged = await self.db.get_anti_farm_flagged(guild.id)

        if not deltas:
            # No tracked users in this guild yet — just update the reset
            # timestamp so we don't try again until next week.
            async with self.db.lock:
                await self.db.set_guild_config(
                    guild.id, last_weekly_reset_ts=week_start_ts
                )
            return

        # Users eligible for VC / Streaming rankings: exclude farmers
        vc_eligible = [row for row in deltas if row[0] not in flagged]

        # Find winners (only award roles for non-zero activity)
        # Tuple layout: (user_id, voice_delta, stream_delta, msg_delta)
        winners: dict[str, int] = {}

        if vc_eligible:
            top_streamer = max(vc_eligible, key=lambda r: r[2])
            if top_streamer[2] > 0:
                winners["streamer"] = top_streamer[0]

            top_vc = max(vc_eligible, key=lambda r: r[1])
            if top_vc[1] > 0:
                winners["vc"] = top_vc[0]

        if deltas:
            top_chat = max(deltas, key=lambda r: r[3])
            if top_chat[3] > 0:
                winners["chat"] = top_chat[0]

        # Assign Discord roles
        assigned: dict[str, discord.Member] = {}
        for key, user_id in winners.items():
            member = guild.get_member(user_id)
            if member is None:
                continue
            role = await self._get_role(guild, key)
            if role is None:
                log.warning(
                    "Role for '%s' not found in guild %d. "
                    "Create it or set the role ID in .env.",
                    _ROLE_NAMES[key], guild.id,
                )
                continue

            # Remove the role from any current holder
            for holder in list(role.members):
                if holder.id != member.id:
                    try:
                        await holder.remove_roles(
                            role, reason="Nexus weekly role reset"
                        )
                    except discord.HTTPException as exc:
                        log.warning(
                            "Failed to remove '%s' from %s: %s",
                            role.name, holder, exc,
                        )

            # Award to this week's winner
            try:
                await member.add_roles(
                    role, reason="Nexus weekly role assignment"
                )
                assigned[key] = member
                log.info(
                    "Assigned '%s' to %s in guild %d.",
                    _ROLE_NAMES[key], member, guild.id,
                )
            except discord.HTTPException as exc:
                log.warning(
                    "Failed to assign '%s' to %s: %s",
                    _ROLE_NAMES[key], member, exc,
                )

        # Post announcement if configured
        async with self.db.lock:
            cfg = await self.db.get_guild_config(guild.id)

        if cfg.announcements_enabled and cfg.announcement_channel_id:
            channel = guild.get_channel(cfg.announcement_channel_id)
            if isinstance(channel, discord.TextChannel):
                await self._post_announcement(channel, assigned, week_start)
            else:
                log.warning(
                    "Announcement channel %s not found or not a text channel "
                    "in guild %d.",
                    cfg.announcement_channel_id, guild.id,
                )

        # Take snapshot and finalize reset
        async with self.db.lock:
            await self.db.take_weekly_snapshot(guild.id, week_start_ts)
            await self.db.clear_anti_farm_flags(guild.id)
            await self.db.set_guild_config(
                guild.id, last_weekly_reset_ts=week_start_ts
            )

    async def _get_role(
        self, guild: discord.Guild, key: str
    ) -> Optional[discord.Role]:
        """Look up a role by configured ID first, then fall back to name."""
        id_map: dict[str, int] = {
            "streamer": self.config.role_streamer_of_the_week,
            "vc":       self.config.role_vc_dominator,
            "chat":     self.config.role_soul_of_the_chat,
        }
        role_id = id_map.get(key, 0)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                return role
        # Name-based fallback (useful before role IDs are configured)
        return discord.utils.get(guild.roles, name=_ROLE_NAMES.get(key, ""))

    async def _post_announcement(
        self,
        channel: discord.TextChannel,
        assigned: dict[str, discord.Member],
        week_start: datetime.datetime,
    ) -> None:
        embed = discord.Embed(
            title="🏆 Weekly Champions",
            description=(
                f"Results for the week of **{week_start.strftime('%B %d, %Y')}** (UTC)\n"
                "Congratulations to this week's winners!"
            ),
            color=discord.Color.gold(),
        )
        for key in _ROLE_KEYS:
            member = assigned.get(key)
            embed.add_field(
                name=f"{_ROLE_ICONS[key]} {_ROLE_NAMES[key]}",
                value=member.mention if member else "No winner this week",
                inline=False,
            )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("Failed to post weekly announcement in %s: %s", channel, exc)
