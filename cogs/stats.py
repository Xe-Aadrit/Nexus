"""
User-facing commands for reading back tracked stats.

Both prefix (!) and slash (/) versions are provided for every command.

Commands
--------
!stats [@member]                     /stats [member]
    All-time activity stats for a member.

!activityboard <metric>              /activityboard <metric>
    Top-10 leaderboard for a given metric.
    Aliases: !leaderboard

!weeklystats [@member] [days]        /weeklystats [member] [days]
    Activity for the last N days (default 7). Supports any N: 1, 7, 30, …
    Aliases: !ws, !activity

!elitepoints [@member]               /elitepoints [member]
    Elite Points total for a member.
    (Defined in cogs/elite_points.py — listed here for reference only.)
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import Database, UserStats
from cogs.voice_tracking import VoiceTracking


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class Stats(commands.Cog):
    def __init__(
        self, bot: commands.Bot, db: Database, voice_tracking: VoiceTracking
    ):
        self.bot = bot
        self.db = db
        self.voice_tracking = voice_tracking

    # ------------------------------------------------------------------ #
    # !stats / /stats  —  all-time activity for one member
    # ------------------------------------------------------------------ #

    @commands.command(name="stats")
    @commands.guild_only()
    async def stats_prefix(
        self, ctx: commands.Context, member: discord.Member = None
    ) -> None:
        """Show tracked activity stats for yourself or another member."""
        await self._show_stats(ctx, member or ctx.author)

    @app_commands.command(
        name="stats",
        description="Show tracked activity stats for yourself or another member.",
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to check (default: yourself)")
    async def stats_slash(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        await self._show_stats(interaction, member or interaction.user)

    async def _show_stats(
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
            row: UserStats = await self.db.get_user_stats(guild_id, member.id)
        live = await self.voice_tracking.live_seconds(guild_id, member.id)

        embed = discord.Embed(
            title=f"Activity stats — {member.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Voice time",
            value=_fmt_duration(row.voice_seconds + live["voice"]),
            inline=True,
        )
        embed.add_field(
            name="Mic-unmuted time",
            value=_fmt_duration(row.unmuted_seconds + live["unmuted"]),
            inline=True,
        )
        embed.add_field(
            name="Speaking time",
            value=_fmt_duration(row.speaking_seconds + live["speaking"]),
            inline=True,
        )
        embed.add_field(
            name="Streaming time",
            value=_fmt_duration(row.streaming_seconds + live["streaming"]),
            inline=True,
        )
        embed.add_field(
            name="Messages sent", value=str(row.message_count), inline=True
        )
        embed.add_field(
            name="⭐ Elite Points", value=str(row.elite_points), inline=True
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if isinstance(ctx_or_int, commands.Context):
            await ctx_or_int.send(embed=embed)
        else:
            await ctx_or_int.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    # !activityboard / /activityboard  —  top-10 leaderboard
    # ------------------------------------------------------------------ #

    @commands.command(name="activityboard", aliases=["leaderboard"])
    @commands.guild_only()
    async def leaderboard_prefix(
        self, ctx: commands.Context, metric: str = "voice"
    ) -> None:
        """Show the top 10 members for a metric.
        Metrics: voice, unmuted, speaking, streaming, messages, elitepoints"""
        await self._show_leaderboard(ctx, metric)

    @app_commands.command(
        name="activityboard",
        description="Show the top 10 members for a tracked activity metric.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        metric="Metric to rank by: voice, unmuted, speaking, streaming, messages, elitepoints"
    )
    async def leaderboard_slash(
        self,
        interaction: discord.Interaction,
        metric: str = "voice",
    ) -> None:
        await self._show_leaderboard(interaction, metric)

    async def _show_leaderboard(
        self,
        ctx_or_int: commands.Context | discord.Interaction,
        metric: str,
    ) -> None:
        metric_map: dict[str, tuple[str, str]] = {
            "voice":        ("voice_seconds",     "Voice time"),
            "unmuted":      ("unmuted_seconds",   "Mic-unmuted time"),
            "speaking":     ("speaking_seconds",  "Speaking time"),
            "streaming":    ("streaming_seconds", "Streaming time"),
            "messages":     ("message_count",     "Messages sent"),
            "elitepoints":  ("elite_points",      "Elite Points"),
            "ep":           ("elite_points",      "Elite Points"),
        }
        choice = metric_map.get(metric.lower())
        if choice is None:
            keys = [k for k in metric_map if k not in ("ep",)]  # hide alias
            msg = f"Unknown metric `{metric}`. Choose one of: {', '.join(keys)}"
            if isinstance(ctx_or_int, commands.Context):
                await ctx_or_int.send(msg)
            else:
                await ctx_or_int.response.send_message(msg, ephemeral=True)
            return

        column, label = choice
        guild_id = (
            ctx_or_int.guild.id
            if isinstance(ctx_or_int, commands.Context)
            else ctx_or_int.guild_id
        )
        async with self.db.lock:
            rows = await self.db.get_leaderboard(guild_id, column, limit=10)

        if not rows:
            msg = "No data yet."
            if isinstance(ctx_or_int, commands.Context):
                await ctx_or_int.send(msg)
            else:
                await ctx_or_int.response.send_message(msg)
            return

        lines = []
        for i, row in enumerate(rows, start=1):
            value = getattr(row, column)
            if column in ("message_count", "elite_points"):
                display = str(value)
            else:
                display = _fmt_duration(value)
            lines.append(f"**{i}.** <@{row.user_id}> — {display}")

        embed = discord.Embed(
            title=f"Leaderboard — {label}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        if isinstance(ctx_or_int, commands.Context):
            await ctx_or_int.send(embed=embed)
        else:
            await ctx_or_int.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    # !weeklystats / /weeklystats  —  n-day activity window
    # ------------------------------------------------------------------ #

    @commands.command(name="weeklystats", aliases=["ws", "activity"])
    @commands.guild_only()
    async def weeklystats_prefix(
        self,
        ctx: commands.Context,
        member: discord.Member = None,
        days: int = 7,
    ) -> None:
        """Show activity for the last N days (default: 7).
        Usage: !weeklystats [@member] [days]
        Examples: !weeklystats, !weeklystats @user, !weeklystats @user 30"""
        await self._show_weekly_stats(ctx, member or ctx.author, days)

    @app_commands.command(
        name="weeklystats",
        description="Show activity for a given time period (1 day, 7 days, 30 days, or any N).",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        member="The member to check (default: yourself)",
        days="Number of days to look back: 1, 7, 30, or any N (default: 7)",
    )
    async def weeklystats_slash(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        days: int = 7,
    ) -> None:
        await self._show_weekly_stats(
            interaction, member or interaction.user, days
        )

    async def _show_weekly_stats(
        self,
        ctx_or_int: commands.Context | discord.Interaction,
        member: discord.Member,
        days: int,
    ) -> None:
        if days < 1:
            msg = "❌ Days must be at least 1."
            if isinstance(ctx_or_int, commands.Context):
                await ctx_or_int.send(msg)
            else:
                await ctx_or_int.response.send_message(msg, ephemeral=True)
            return

        guild_id = (
            ctx_or_int.guild.id
            if isinstance(ctx_or_int, commands.Context)
            else ctx_or_int.guild_id
        )
        async with self.db.lock:
            deltas = await self.db.get_nday_deltas(guild_id, member.id, days)

        period = f"last {days} day{'s' if days != 1 else ''}"
        embed = discord.Embed(
            title=f"Activity ({period}) — {member.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Voice time",
            value=_fmt_duration(deltas["voice"]),
            inline=True,
        )
        embed.add_field(
            name="Streaming time",
            value=_fmt_duration(deltas["streaming"]),
            inline=True,
        )
        embed.add_field(
            name="Messages sent",
            value=str(deltas["messages"]),
            inline=True,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(
            text="Note: data availability depends on how long Nexus has been running."
        )

        if isinstance(ctx_or_int, commands.Context):
            await ctx_or_int.send(embed=embed)
        else:
            await ctx_or_int.response.send_message(embed=embed)