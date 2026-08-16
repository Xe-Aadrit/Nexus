"""
Admin commands for configuring Nexus per-guild settings.

All commands require the Manage Guild permission. Both prefix (!) and slash
(/) versions are provided for every command.

Commands
--------
!antispam on|off                — Toggle Anti Spam
/antispam <state>

!antifarm on|off                — Toggle Anti Farm
/antifarm <state>

!spamconfig <count> <seconds>   — Set spam detection window
/spamconfig <count> <seconds>     (e.g. 5 messages in 5 seconds)

!setannouncechannel [#channel]  — Set weekly announcement channel
/setannouncechannel [channel]     (defaults to current channel)

!announcements on|off           — Toggle weekly announcements
/announcements <state>

!setvotechannel [#channel]      — Set channel where Vote Tracker posts
/setvotechannel [channel]         (defaults to current channel)

!nexusconfig                    — Show current server configuration
/nexusconfig
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_toggle(state: str) -> Optional[bool]:
    """Parse an on/off string to bool, or None if unrecognised."""
    if state.strip().lower() in ("on", "true", "1", "yes", "enable", "enabled"):
        return True
    if state.strip().lower() in ("off", "false", "0", "no", "disable", "disabled"):
        return False
    return None


def _manage_guild_check():
    """Prefix-command check: caller must have Manage Guild permission."""
    async def predicate(ctx: commands.Context) -> bool:
        return bool(ctx.guild and ctx.author.guild_permissions.manage_guild)
    return commands.check(predicate)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------ #
    # Internal utilities
    # ------------------------------------------------------------------ #

    def _has_manage_guild(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )

    async def _deny_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "❌ You need the **Manage Guild** permission to use this command.",
            ephemeral=True,
        )

    async def _reply(
        self,
        ctx_or_int: commands.Context | discord.Interaction,
        content: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        if isinstance(ctx_or_int, commands.Context):
            await ctx_or_int.send(content)
        else:
            await ctx_or_int.response.send_message(content, ephemeral=ephemeral)

    def _guild_id(
        self, ctx_or_int: commands.Context | discord.Interaction
    ) -> int:
        if isinstance(ctx_or_int, commands.Context):
            return ctx_or_int.guild.id
        return ctx_or_int.guild_id

    # ------------------------------------------------------------------ #
    # Anti Spam toggle
    # ------------------------------------------------------------------ #

    @commands.command(name="antispam")
    @commands.guild_only()
    @_manage_guild_check()
    async def antispam_prefix(
        self, ctx: commands.Context, state: str
    ) -> None:
        """Toggle Anti Spam on or off. Usage: !antispam on|off"""
        await self._toggle_antispam(ctx, state)

    @app_commands.command(
        name="antispam", description="Toggle Anti Spam on or off."
    )
    @app_commands.guild_only()
    @app_commands.describe(state="on or off")
    async def antispam_slash(
        self, interaction: discord.Interaction, state: str
    ) -> None:
        if not self._has_manage_guild(interaction):
            await self._deny_slash(interaction)
            return
        await self._toggle_antispam(interaction, state)

    async def _toggle_antispam(
        self, ctx_or_int: commands.Context | discord.Interaction, state: str
    ) -> None:
        enabled = _parse_toggle(state)
        if enabled is None:
            await self._reply(ctx_or_int, "❌ Please specify `on` or `off`.")
            return
        async with self.db.lock:
            await self.db.set_guild_config(
                self._guild_id(ctx_or_int), anti_spam_enabled=int(enabled)
            )
        status = "✅ **enabled**" if enabled else "🔴 **disabled**"
        await self._reply(ctx_or_int, f"Anti Spam is now {status}.")

    # ------------------------------------------------------------------ #
    # Anti Farm toggle
    # ------------------------------------------------------------------ #

    @commands.command(name="antifarm")
    @commands.guild_only()
    @_manage_guild_check()
    async def antifarm_prefix(
        self, ctx: commands.Context, state: str
    ) -> None:
        """Toggle Anti Farm on or off. Usage: !antifarm on|off"""
        await self._toggle_antifarm(ctx, state)

    @app_commands.command(
        name="antifarm", description="Toggle Anti Farm on or off."
    )
    @app_commands.guild_only()
    @app_commands.describe(state="on or off")
    async def antifarm_slash(
        self, interaction: discord.Interaction, state: str
    ) -> None:
        if not self._has_manage_guild(interaction):
            await self._deny_slash(interaction)
            return
        await self._toggle_antifarm(interaction, state)

    async def _toggle_antifarm(
        self, ctx_or_int: commands.Context | discord.Interaction, state: str
    ) -> None:
        enabled = _parse_toggle(state)
        if enabled is None:
            await self._reply(ctx_or_int, "❌ Please specify `on` or `off`.")
            return
        async with self.db.lock:
            await self.db.set_guild_config(
                self._guild_id(ctx_or_int), anti_farm_enabled=int(enabled)
            )
        status = "✅ **enabled**" if enabled else "🔴 **disabled**"
        await self._reply(ctx_or_int, f"Anti Farm is now {status}.")

    # ------------------------------------------------------------------ #
    # Anti Spam window configuration
    # ------------------------------------------------------------------ #

    @commands.command(name="spamconfig")
    @commands.guild_only()
    @_manage_guild_check()
    async def spamconfig_prefix(
        self, ctx: commands.Context, msg_limit: int, window_seconds: int
    ) -> None:
        """Set the Anti Spam detection window.
        Usage: !spamconfig <messages> <seconds>
        Example: !spamconfig 5 5  →  5 messages within 5 seconds = spam."""
        await self._set_spamconfig(ctx, msg_limit, window_seconds)

    @app_commands.command(
        name="spamconfig",
        description="Set the Anti Spam detection window (messages and seconds).",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        msg_limit="Number of messages within the window that triggers spam detection",
        window_seconds="Duration of the sliding window in seconds",
    )
    async def spamconfig_slash(
        self,
        interaction: discord.Interaction,
        msg_limit: int,
        window_seconds: int,
    ) -> None:
        if not self._has_manage_guild(interaction):
            await self._deny_slash(interaction)
            return
        await self._set_spamconfig(interaction, msg_limit, window_seconds)

    async def _set_spamconfig(
        self,
        ctx_or_int: commands.Context | discord.Interaction,
        msg_limit: int,
        window_seconds: int,
    ) -> None:
        if msg_limit < 1 or window_seconds < 1:
            await self._reply(ctx_or_int, "❌ Both values must be at least 1.")
            return
        async with self.db.lock:
            await self.db.set_guild_config(
                self._guild_id(ctx_or_int),
                anti_spam_msg_limit=msg_limit,
                anti_spam_window_seconds=window_seconds,
            )
        await self._reply(
            ctx_or_int,
            f"✅ Spam detection set to **{msg_limit} messages** "
            f"within **{window_seconds} second{'s' if window_seconds != 1 else ''}**.",
        )

    # ------------------------------------------------------------------ #
    # Announcement channel
    # ------------------------------------------------------------------ #

    @commands.command(name="setannouncechannel")
    @commands.guild_only()
    @_manage_guild_check()
    async def setannouncechannel_prefix(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel = None,
    ) -> None:
        """Set the channel for weekly role announcements.
        Usage: !setannouncechannel #channel  (defaults to current channel)"""
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("❌ That's not a text channel.")
            return
        await self._set_announce_channel(ctx, target)

    @app_commands.command(
        name="setannouncechannel",
        description="Set the channel for weekly role announcements.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The channel to post weekly announcements in (default: current channel)"
    )
    async def setannouncechannel_slash(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not self._has_manage_guild(interaction):
            await self._deny_slash(interaction)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "❌ That's not a text channel.", ephemeral=True
            )
            return
        await self._set_announce_channel(interaction, target)

    async def _set_announce_channel(
        self,
        ctx_or_int: commands.Context | discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        async with self.db.lock:
            await self.db.set_guild_config(
                self._guild_id(ctx_or_int),
                announcement_channel_id=channel.id,
            )
        await self._reply(
            ctx_or_int,
            f"✅ Weekly role announcements will be posted in {channel.mention}.",
        )

    # ------------------------------------------------------------------ #
    # Announcements toggle
    # ------------------------------------------------------------------ #

    @commands.command(name="announcements")
    @commands.guild_only()
    @_manage_guild_check()
    async def announcements_prefix(
        self, ctx: commands.Context, state: str
    ) -> None:
        """Toggle weekly role announcements. Usage: !announcements on|off"""
        await self._toggle_announcements(ctx, state)

    @app_commands.command(
        name="announcements",
        description="Toggle weekly role announcements on or off.",
    )
    @app_commands.guild_only()
    @app_commands.describe(state="on or off")
    async def announcements_slash(
        self, interaction: discord.Interaction, state: str
    ) -> None:
        if not self._has_manage_guild(interaction):
            await self._deny_slash(interaction)
            return
        await self._toggle_announcements(interaction, state)

    async def _toggle_announcements(
        self, ctx_or_int: commands.Context | discord.Interaction, state: str
    ) -> None:
        enabled = _parse_toggle(state)
        if enabled is None:
            await self._reply(ctx_or_int, "❌ Please specify `on` or `off`.")
            return
        async with self.db.lock:
            await self.db.set_guild_config(
                self._guild_id(ctx_or_int), announcements_enabled=int(enabled)
            )
        status = "✅ **enabled**" if enabled else "🔴 **disabled**"
        await self._reply(ctx_or_int, f"Weekly announcements are now {status}.")

    # ------------------------------------------------------------------ #
    # Vote channel
    # ------------------------------------------------------------------ #

    @commands.command(name="setvotechannel")
    @commands.guild_only()
    @_manage_guild_check()
    async def setvotechannel_prefix(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel = None,
    ) -> None:
        """Set the channel where the Top.gg Vote Tracker bot posts.
        Usage: !setvotechannel #channel  (defaults to current channel)"""
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("❌ That's not a text channel.")
            return
        await self._set_vote_channel(ctx, target)

    @app_commands.command(
        name="setvotechannel",
        description="Set the channel where the Top.gg Vote Tracker bot posts.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The channel where Vote Tracker posts vote notifications "
                "(default: current channel)"
    )
    async def setvotechannel_slash(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not self._has_manage_guild(interaction):
            await self._deny_slash(interaction)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "❌ That's not a text channel.", ephemeral=True
            )
            return
        await self._set_vote_channel(interaction, target)

    async def _set_vote_channel(
        self,
        ctx_or_int: commands.Context | discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        async with self.db.lock:
            await self.db.set_guild_config(
                self._guild_id(ctx_or_int), vote_channel_id=channel.id
            )
        await self._reply(
            ctx_or_int,
            f"✅ Vote Tracker channel set to {channel.mention}. "
            "Elite Points will be awarded for votes posted there.",
        )

    # ------------------------------------------------------------------ #
    # Config overview
    # ------------------------------------------------------------------ #

    @commands.command(name="nexusconfig")
    @commands.guild_only()
    @_manage_guild_check()
    async def nexusconfig_prefix(self, ctx: commands.Context) -> None:
        """Show the current Nexus configuration for this server."""
        await self._show_config(ctx)

    @app_commands.command(
        name="nexusconfig",
        description="Show the current Nexus configuration for this server.",
    )
    @app_commands.guild_only()
    async def nexusconfig_slash(self, interaction: discord.Interaction) -> None:
        if not self._has_manage_guild(interaction):
            await self._deny_slash(interaction)
            return
        await self._show_config(interaction)

    async def _show_config(
        self, ctx_or_int: commands.Context | discord.Interaction
    ) -> None:
        guild_id = self._guild_id(ctx_or_int)
        guild: discord.Guild = (
            ctx_or_int.guild
            if isinstance(ctx_or_int, commands.Context)
            else ctx_or_int.guild
        )

        async with self.db.lock:
            cfg = await self.db.get_guild_config(guild_id)

        ann_ch = (
            guild.get_channel(cfg.announcement_channel_id)
            if cfg.announcement_channel_id
            else None
        )
        vote_ch = (
            guild.get_channel(cfg.vote_channel_id)
            if cfg.vote_channel_id
            else None
        )

        embed = discord.Embed(
            title="⚙️ Nexus Configuration",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Anti Spam",
            value=(
                f"{'✅' if cfg.anti_spam_enabled else '🔴'} "
                f"{'Enabled' if cfg.anti_spam_enabled else 'Disabled'}\n"
                f"Window: **{cfg.anti_spam_msg_limit}** msgs / "
                f"**{cfg.anti_spam_window_seconds}** s"
            ),
            inline=True,
        )
        embed.add_field(
            name="Anti Farm",
            value=(
                f"{'✅' if cfg.anti_farm_enabled else '🔴'} "
                f"{'Enabled' if cfg.anti_farm_enabled else 'Disabled'}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Weekly Announcements",
            value=(
                f"{'✅' if cfg.announcements_enabled else '🔴'} "
                f"{'Enabled' if cfg.announcements_enabled else 'Disabled'}\n"
                f"Channel: {ann_ch.mention if ann_ch else '*(not set)*'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Vote Channel",
            value=vote_ch.mention if vote_ch else "*(not set)*",
            inline=True,
        )

        if isinstance(ctx_or_int, commands.Context):
            await ctx_or_int.send(embed=embed)
        else:
            await ctx_or_int.response.send_message(embed=embed, ephemeral=True)
