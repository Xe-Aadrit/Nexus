"""
Entry point.

Run with:  python bot.py
(after copying .env.example to .env and filling in DISCORD_TOKEN)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord
from discord.ext import commands

from config import load_config
from database import Database
from cogs.voice_tracking import VoiceTracking
from cogs.message_tracking import MessageTracking
from cogs.stats import Stats
from cogs.elite_points import ElitePoints
from cogs.weekly_roles import WeeklyRoles
from cogs.anti_spam import AntiSpam
from cogs.anti_farm import AntiFarm
from cogs.admin import Admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("nexus")


class ActivityBot(commands.Bot):
    def __init__(self, config, db: Database):
        # --- Gateway intents ---
        # guilds:         populates the channel/thread caches; required for
        #                 essentially any bot to function. Not privileged.
        # voice_states:   required for join/leave/move/mute/deafen/stream
        #                 events. Not privileged.
        # guild_messages: required to receive on_message so we can count
        #                 messages. Not privileged.
        # members:        PRIVILEGED — required to look up guild members by
        #                 ID (needed when assigning weekly roles and resolving
        #                 voter names from Vote Tracker messages). Must be
        #                 enabled in the Discord Developer Portal under
        #                 Bot → Privileged Gateway Intents.
        #
        # We deliberately do NOT request message_content or presences.
        intents = discord.Intents.none()
        intents.message_content = True
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.members = True  # Privileged: needed for role assignment

        super().__init__(command_prefix=config.command_prefix, intents=intents)
        self.config = config
        self.db = db
        self.voice_tracking: VoiceTracking | None = None

    async def setup_hook(self) -> None:
        await self.db.connect()

        # Core tracking cogs (order matters: AntiSpam before MessageTracking
        # so the cog is available via bot.cogs when on_message fires)
        anti_spam = AntiSpam(self, self.db)
        await self.add_cog(anti_spam)

        voice_tracking = VoiceTracking(
            self,
            self.db,
            flush_interval=self.config.flush_interval_seconds,
            track_afk=self.config.track_afk_channel,
        )
        self.voice_tracking = voice_tracking
        await self.add_cog(voice_tracking)
        await self.add_cog(MessageTracking(self, self.db))
        await self.add_cog(Stats(self, self.db, voice_tracking))

        # Optional: real speaking-time detection
        if self.config.enable_speaking_tracking:
            try:
                from cogs.speaking_tracking import SpeakingTracking

                await self.add_cog(
                    SpeakingTracking(
                        self,
                        voice_tracking,
                        min_members=self.config.speaking_min_members,
                        idle_timeout=self.config.speaking_idle_timeout_seconds,
                    )
                )
                log.info("Speaking-time tracking enabled.")
            except (RuntimeError, ImportError) as exc:
                log.warning(
                    "ENABLE_SPEAKING_TRACKING was set but the feature could not "
                    "be loaded (%s). Continuing without it. See README.md for "
                    "the extra install steps.",
                    exc,
                )

        # New feature cogs
        await self.add_cog(ElitePoints(self, self.db, self.config))
        await self.add_cog(WeeklyRoles(self, self.db, self.config))
        await self.add_cog(AntiFarm(self, self.db, self.config))
        await self.add_cog(Admin(self, self.db))

        # Sync slash commands
        if self.config.guild_id:
            # Guild-scoped sync: instant, good for development/testing
            guild_obj = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            log.info(
                "Slash commands synced to guild %d (instant).",
                self.config.guild_id,
            )
        else:
            # Global sync: takes up to 1 hour to propagate across Discord
            await self.tree.sync()
            log.info("Slash commands synced globally.")

    async def close(self) -> None:
        # Idempotent: safe even if called more than once (e.g. once from an
        # explicit shutdown-signal handler and again via `async with bot:`
        # tearing down).
        log.info("Shutting down — flushing in-progress voice sessions...")
        if self.voice_tracking is not None:
            await self.voice_tracking.shutdown()
        await self.db.close()
        await super().close()


async def main() -> None:
    config = load_config()
    db = Database(config.database_path)
    bot = ActivityBot(config, db)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    # SIGTERM (e.g. `docker stop`, systemd) doesn't raise KeyboardInterrupt
    # like SIGINT does, so without this a container stop would skip our
    # final flush and rely solely on the periodic-flush bound. Handling it
    # explicitly gets us an exact flush on ordinary restarts/deploys.
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

    async with bot:
        bot_task = asyncio.create_task(bot.start(config.token))

        if sys.platform != "win32":
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if stop_task in done:
                log.info("Shutdown signal received.")
                await bot.close()
            elif bot_task.exception() is not None:
                raise bot_task.exception()
        else:
            # add_signal_handler isn't available on Windows; just await
            # normally and rely on KeyboardInterrupt for Ctrl+C.
            await bot_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass