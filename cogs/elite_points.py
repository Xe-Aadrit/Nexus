"""
Elite Points distribution via Top.gg Vote Tracker bot messages.

The Top.gg Vote Tracker Discord bot posts a notification message in a
designated channel whenever a server member votes for the bot on Top.gg.
This cog monitors that channel, parses the voter's display name from the
message, resolves it to a guild member, and awards Elite Points.

Setup (one-time, per server):
  1. Set TOPGG_BOT_ID in .env to the Vote Tracker bot's Discord user ID.
     (Developer Mode → right-click the Vote Tracker bot → Copy User ID)
  2. Run !setvotechannel #your-channel (or /setvotechannel) to tell Nexus
     which channel the Vote Tracker posts in.

Limitations:
  The Vote Tracker bot posts the voter's *display name*, not their user ID.
  Member resolution is a best-effort match against display names and usernames.
  If the name is ambiguous (multiple members share it) or not found, the vote
  is logged as a warning but no points are awarded. No sensitive data is stored.

Commands:
  !elitepoints [@member]  /elitepoints [member]  — Show a member's Elite Points
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import Database
from config import Config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vote message parsing
# ---------------------------------------------------------------------------

# The Top.gg Vote Tracker bot typically sends messages in one of these forms:
#   "Username just voted for BotName!"
#   "**Username** just voted for BotName!"
#   "Username has voted for BotName!"
#   Embed descriptions with similar patterns
# We try all patterns against the first line of the message / embed text.
_VOTE_PATTERNS = [
    re.compile(r"^\*{0,2}(.+?)\*{0,2} just voted", re.IGNORECASE),
    re.compile(r"^\*{0,2}(.+?)\*{0,2} has voted",  re.IGNORECASE),
    re.compile(r"^\*{0,2}(.+?)\*{0,2} voted",       re.IGNORECASE),
]


def _extract_voter_name(text: str) -> Optional[str]:
    """Extract the voter's display name from a Vote Tracker message."""
    first_line = text.strip().split("\n")[0]
    for pattern in _VOTE_PATTERNS:
        m = pattern.match(first_line)
        if m:
            name = m.group(1).strip().lstrip("@").strip()
            return name if name else None
    return None


def _resolve_member(
    guild: discord.Guild, name: str
) -> Optional[discord.Member]:
    """Best-effort lookup: match display name or username (case-insensitive).
    Returns None if no match or if the match is ambiguous."""
    name_lower = name.lower()
    matches = [
        m for m in guild.members
        if not m.bot
        and (
            m.display_name.lower() == name_lower
            or m.name.lower() == name_lower
        )
    ]
    if len(matches) == 1:
        return matches[0]
    return None  # not found or ambiguous


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ElitePoints(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database, config: Config):
        self.bot = bot
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ #
    # Vote detection
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Watch for Vote Tracker messages and award Elite Points."""
        # Only look at messages from the configured Vote Tracker bot
        if self.config.topgg_bot_id == 0:
            return
        if message.author.id != self.config.topgg_bot_id:
            return
        if message.guild is None:
            return

        async with self.db.lock:
            cfg = await self.db.get_guild_config(message.guild.id)

        if cfg.vote_channel_id is None or message.channel.id != cfg.vote_channel_id:
            return

        # Try to extract the voter name from plain content or embeds
        voter_name: Optional[str] = None

        if message.content:
            voter_name = _extract_voter_name(message.content)

        if voter_name is None:
            for embed in message.embeds:
                candidate = (embed.description or "") + " " + (embed.title or "")
                voter_name = _extract_voter_name(candidate.strip())
                if voter_name:
                    break

        if voter_name is None:
            log.warning(
                "Vote Tracker message in guild %d could not be parsed "
                "(content: %r). No Elite Points awarded.",
                message.guild.id,
                (message.content or "")[:200],
            )
            return

        member = _resolve_member(message.guild, voter_name)
        if member is None:
            log.warning(
                "Could not resolve voter name %r to a unique member in "
                "guild %d. No Elite Points awarded.",
                voter_name,
                message.guild.id,
            )
            return

        points = self.config.elite_points_per_vote
        async with self.db.lock:
            await self.db.add_elite_points(message.guild.id, member.id, points)

        log.info(
            "Awarded %d Elite Point(s) to %s (guild %d) for voting on Top.gg.",
            points,
            member,
            message.guild.id,
        )

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    @commands.command(name="elitepoints", aliases=["ep", "points"])
    @commands.guild_only()
    async def elitepoints_prefix(
        self, ctx: commands.Context, member: discord.Member = None
    ) -> None:
        """Show Elite Points for yourself or another member."""
        await self._show_elite_points(ctx, member or ctx.author)

    @app_commands.command(
        name="elitepoints",
        description="Show Elite Points for yourself or another member.",
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to check (default: yourself)")
    async def elitepoints_slash(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        target = member or interaction.user
        await self._show_elite_points(interaction, target)

    async def _show_elite_points(
        self,
        ctx_or_int: commands.Context | discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild_id = (
            ctx_or_int.guild.id
            if isinstance(ctx_or_int, commands.Context)
            else ctx_or_int.guild_id
        )
        async with self.db.lock:
            stats = await self.db.get_user_stats(guild_id, member.id)

        embed = discord.Embed(
            title=f"⭐ Elite Points — {member.display_name}",
            description=f"**{stats.elite_points}** Elite Point{'s' if stats.elite_points != 1 else ''}",
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if isinstance(ctx_or_int, commands.Context):
            await ctx_or_int.send(embed=embed)
        else:
            await ctx_or_int.response.send_message(embed=embed)
