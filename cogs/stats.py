"""
User-facing commands for reading back the tracked stats.

Only needs Send Messages + Embed Links in the channel it's used in - no
elevated permissions.
"""
from __future__ import annotations

import discord
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
    def __init__(self, bot: commands.Bot, db: Database, voice_tracking: VoiceTracking):
        self.bot = bot
        self.db = db
        self.voice_tracking = voice_tracking

    @commands.command(name="stats")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context, member: discord.Member = None) -> None:
        """Show tracked activity stats for yourself or another member."""
        member = member or ctx.author
        async with self.db.lock:
            row: UserStats = await self.db.get_user_stats(ctx.guild.id, member.id)
        live = await self.voice_tracking.live_seconds(ctx.guild.id, member.id)

        embed = discord.Embed(
            title=f"Activity stats for {member.display_name}",
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
        embed.add_field(name="Messages sent", value=str(row.message_count), inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="activityboard", aliases=["leaderboard"])
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context, metric: str = "voice") -> None:
        """Show the top 10 members for a metric: voice, unmuted, speaking, streaming, messages."""
        metric_map = {
            "voice": ("voice_seconds", "Voice time"),
            "unmuted": ("unmuted_seconds", "Mic-unmuted time"),
            "speaking": ("speaking_seconds", "Speaking time"),
            "streaming": ("streaming_seconds", "Streaming time"),
            "messages": ("message_count", "Messages sent"),
        }
        choice = metric_map.get(metric.lower())
        if choice is None:
            await ctx.send(
                f"Unknown metric `{metric}`. Choose one of: {', '.join(metric_map)}"
            )
            return
        column, label = choice

        async with self.db.lock:
            rows = await self.db.get_leaderboard(ctx.guild.id, column, limit=10)

        if not rows:
            await ctx.send("No data yet.")
            return

        lines = []
        for i, row in enumerate(rows, start=1):
            value = row.message_count if column == "message_count" else getattr(row, column)
            display = str(value) if column == "message_count" else _fmt_duration(value)
            lines.append(f"**{i}.** <@{row.user_id}> - {display}")

        embed = discord.Embed(
            title=f"Leaderboard - {label}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)