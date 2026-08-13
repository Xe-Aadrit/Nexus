"""
Configuration loading for the activity-tracking bot.

All configuration comes from environment variables (optionally loaded from a
.env file via python-dotenv). Keeping this in one place makes it easy to see
every knob the bot exposes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file; real deployments can use real env vars


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


@dataclass(frozen=True)
class Config:
    # --- Required ---
    token: str

    # --- Database ---
    database_path: str = "activity_stats.sqlite3"

    # --- Commands ---
    command_prefix: str = "!"

    # --- Behaviour tuning ---
    # How often (seconds) in-memory voice/mic/stream timers are flushed to the
    # database. This bounds how much data could be lost if the process is
    # killed without a clean shutdown (crash, `kill -9`, host reboot, etc).
    flush_interval_seconds: int = _get_int("FLUSH_INTERVAL_SECONDS", 30)

    # Whether to track time spent in a guild's configured AFK channel.
    # Default is False: being pushed to the AFK channel for inactivity isn't
    # really "voice activity".
    track_afk_channel: bool = _get_bool("TRACK_AFK_CHANNEL", False)

    # --- Optional: real speaking-time detection ---
    # This requires the bot to actually join a voice channel and an
    # experimental third-party library (discord-ext-voice-recv). See the
    # README for the tradeoffs before enabling this. Off by default.
    enable_speaking_tracking: bool = _get_bool("ENABLE_SPEAKING_TRACKING", False)

    # Minimum number of non-bot humans that must be in a voice channel before
    # the bot will bother joining it to measure speaking activity.
    speaking_min_members: int = _get_int("SPEAKING_MIN_MEMBERS", 1)

    # Seconds to wait after a target channel becomes empty before the bot
    # disconnects (avoids join/leave thrashing).
    speaking_idle_timeout_seconds: int = _get_int("SPEAKING_IDLE_TIMEOUT_SECONDS", 15)


def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill in your "
            "bot token, or export DISCORD_TOKEN in your environment."
        )
    return Config(
        token=token,
        database_path=os.getenv("DATABASE_PATH", "activity_stats.sqlite3"),
        command_prefix=os.getenv("COMMAND_PREFIX", "!"),
    )