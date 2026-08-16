"""
Persistence layer.

Everything goes through a single aiosqlite connection guarded by an
asyncio.Lock. SQLite serialises writes internally anyway, so this doesn't
cost real concurrency, but it does give us clean atomic
"read-modify-write" sequences without reaching for a heavier database.

Schema
------
user_stats
    One row per (guild_id, user_id). Holds cumulative all-time totals.
    This is the "source of truth" that survives restarts.

active_sessions
    One row per (guild_id, user_id) currently in a voice channel. Stores
    the wall-clock timestamps (epoch seconds) at which each currently-open
    timer (voice / unmuted / speaking / streaming) last started or was
    checkpointed. Rows are deleted when a user leaves voice. This table
    exists purely so that a crash doesn't lose more than one flush
    interval's worth of data (see cogs/voice_tracking.py) and so a clean
    restart can tell which sessions were still open when it went down.

weekly_snapshots
    One row per (guild_id, user_id). Stores the cumulative totals at the
    start of the current week (Monday 00:00 UTC). Weekly deltas are
    computed as current_total - snapshot. Overwritten at each weekly reset.

daily_snapshots
    One row per (guild_id, user_id, snapshot_date). Stores midnight-UTC
    snapshots for up to 31 days, enabling n-day activity window queries
    (1 day, 7 days, 30 days, or any arbitrary N days).

guild_config
    One row per guild_id. Stores per-guild toggles (Anti Spam, Anti Farm,
    announcements) and configurable parameters.

anti_farm_flags
    One row per (guild_id, user_id). Users flagged here are excluded from
    VC Dominator / Streamer Of The Week rankings in the current week.
    Cleared at each weekly reset.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema — all CREATE TABLE statements use IF NOT EXISTS so they are safe
# to run on both fresh and existing databases.
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS user_stats (
    guild_id           INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    voice_seconds      REAL    NOT NULL DEFAULT 0,
    unmuted_seconds    REAL    NOT NULL DEFAULT 0,
    speaking_seconds   REAL    NOT NULL DEFAULT 0,
    streaming_seconds  REAL    NOT NULL DEFAULT 0,
    message_count      INTEGER NOT NULL DEFAULT 0,
    elite_points       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS active_sessions (
    guild_id             INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    channel_id           INTEGER NOT NULL,
    voice_started_at     REAL    NOT NULL,
    unmuted_started_at   REAL,
    speaking_started_at  REAL,
    streaming_started_at REAL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS weekly_snapshots (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    snap_voice     REAL    NOT NULL DEFAULT 0,
    snap_streaming REAL    NOT NULL DEFAULT 0,
    snap_messages  INTEGER NOT NULL DEFAULT 0,
    week_start_ts  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    snapshot_date  REAL    NOT NULL,
    snap_voice     REAL    NOT NULL DEFAULT 0,
    snap_streaming REAL    NOT NULL DEFAULT 0,
    snap_messages  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id                 INTEGER PRIMARY KEY,
    anti_spam_enabled        INTEGER NOT NULL DEFAULT 1,
    anti_farm_enabled        INTEGER NOT NULL DEFAULT 1,
    announcements_enabled    INTEGER NOT NULL DEFAULT 0,
    announcement_channel_id  INTEGER,
    vote_channel_id          INTEGER,
    anti_spam_msg_limit      INTEGER NOT NULL DEFAULT 5,
    anti_spam_window_seconds INTEGER NOT NULL DEFAULT 5,
    last_weekly_reset_ts     REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS anti_farm_flags (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    flagged_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""

# ---------------------------------------------------------------------------
# Idempotent migrations for existing databases that predate this upgrade.
# SQLite does not support ADD COLUMN IF NOT EXISTS, so we catch the
# OperationalError that fires when the column already exists.
# ---------------------------------------------------------------------------
_MIGRATIONS = [
    "ALTER TABLE user_stats ADD COLUMN elite_points INTEGER NOT NULL DEFAULT 0",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class UserStats:
    guild_id: int
    user_id: int
    voice_seconds: float = 0.0
    unmuted_seconds: float = 0.0
    speaking_seconds: float = 0.0
    streaming_seconds: float = 0.0
    message_count: int = 0
    elite_points: int = 0


@dataclass
class SessionRow:
    guild_id: int
    user_id: int
    channel_id: int
    voice_started_at: float
    unmuted_started_at: Optional[float]
    speaking_started_at: Optional[float]
    streaming_started_at: Optional[float]


@dataclass
class GuildConfig:
    guild_id: int
    anti_spam_enabled: bool = True
    anti_farm_enabled: bool = True
    announcements_enabled: bool = False
    announcement_channel_id: Optional[int] = None
    vote_channel_id: Optional[int] = None
    anti_spam_msg_limit: int = 5
    anti_spam_window_seconds: int = 5
    last_weekly_reset_ts: float = 0.0


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        # WAL mode gives better durability/concurrency characteristics for a
        # long-running process that writes frequently.
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._apply_migrations()

    async def _apply_migrations(self) -> None:
        """Apply idempotent schema migrations for existing databases."""
        for sql in _MIGRATIONS:
            try:
                await self._conn.execute(sql)
                await self._conn.commit()
            except aiosqlite.OperationalError:
                pass  # Column or change already exists — skip

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() was not called"
        return self._conn

    # ------------------------------------------------------------------ #
    # user_stats
    # ------------------------------------------------------------------ #

    async def ensure_user_row(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO user_stats (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )

    async def add_deltas(
        self,
        guild_id: int,
        user_id: int,
        *,
        voice: float = 0.0,
        unmuted: float = 0.0,
        speaking: float = 0.0,
        streaming: float = 0.0,
        messages: int = 0,
    ) -> None:
        """Atomically add (possibly fractional) seconds / message counts.

        Seconds are stored as REAL and accumulated exactly (no per-call
        rounding). This matters: voice_tracking.py flushes frequently
        (potentially many sub-one-second deltas from rapid mute/unmute
        toggling, channel moves, etc.), and rounding each individual delta
        to the nearest integer before adding it would silently lose real
        time - e.g. ten flushes of 0.4s each would round-trip to 0 total
        instead of 4 seconds. Rounding only happens for display, in
        cogs/stats.py.
        """
        if not any((voice, unmuted, speaking, streaming, messages)):
            return
        await self.ensure_user_row(guild_id, user_id)
        await self.conn.execute(
            """
            UPDATE user_stats
            SET voice_seconds     = voice_seconds     + ?,
                unmuted_seconds   = unmuted_seconds   + ?,
                speaking_seconds  = speaking_seconds  + ?,
                streaming_seconds = streaming_seconds + ?,
                message_count     = message_count     + ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (voice, unmuted, speaking, streaming, messages, guild_id, user_id),
        )
        await self.conn.commit()

    async def add_elite_points(
        self, guild_id: int, user_id: int, amount: int
    ) -> None:
        """Add Elite Points to a user's all-time total."""
        if amount <= 0:
            return
        await self.ensure_user_row(guild_id, user_id)
        await self.conn.execute(
            """
            UPDATE user_stats
            SET elite_points = elite_points + ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (amount, guild_id, user_id),
        )
        await self.conn.commit()

    async def get_user_stats(self, guild_id: int, user_id: int) -> UserStats:
        cur = await self.conn.execute(
            """
            SELECT voice_seconds, unmuted_seconds, speaking_seconds,
                   streaming_seconds, message_count, elite_points
            FROM user_stats WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return UserStats(guild_id=guild_id, user_id=user_id)
        return UserStats(guild_id, user_id, *row)

    async def get_leaderboard(
        self, guild_id: int, order_by: str, limit: int = 10
    ) -> list[UserStats]:
        allowed = {
            "voice_seconds",
            "unmuted_seconds",
            "speaking_seconds",
            "streaming_seconds",
            "message_count",
            "elite_points",
        }
        if order_by not in allowed:
            raise ValueError(f"invalid order_by: {order_by}")
        cur = await self.conn.execute(
            f"""
            SELECT user_id, voice_seconds, unmuted_seconds, speaking_seconds,
                   streaming_seconds, message_count, elite_points
            FROM user_stats
            WHERE guild_id = ?
            ORDER BY {order_by} DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cur.fetchall()
        return [
            UserStats(guild_id, user_id, *rest) for (user_id, *rest) in rows
        ]

    # ------------------------------------------------------------------ #
    # active_sessions (crash-recovery checkpoints)
    # ------------------------------------------------------------------ #

    async def upsert_session(self, s: SessionRow) -> None:
        await self.conn.execute(
            """
            INSERT INTO active_sessions
                (guild_id, user_id, channel_id, voice_started_at,
                 unmuted_started_at, speaking_started_at, streaming_started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                channel_id           = excluded.channel_id,
                voice_started_at     = excluded.voice_started_at,
                unmuted_started_at   = excluded.unmuted_started_at,
                speaking_started_at  = excluded.speaking_started_at,
                streaming_started_at = excluded.streaming_started_at
            """,
            (
                s.guild_id,
                s.user_id,
                s.channel_id,
                s.voice_started_at,
                s.unmuted_started_at,
                s.speaking_started_at,
                s.streaming_started_at,
            ),
        )
        await self.conn.commit()

    async def delete_session(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM active_sessions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()

    async def all_sessions(self) -> list[SessionRow]:
        cur = await self.conn.execute(
            """
            SELECT guild_id, user_id, channel_id, voice_started_at,
                   unmuted_started_at, speaking_started_at, streaming_started_at
            FROM active_sessions
            """
        )
        rows = await cur.fetchall()
        return [SessionRow(*row) for row in rows]

    async def clear_all_sessions(self) -> None:
        """Wipe leftover session checkpoints. Called at startup after any
        stale sessions have been flushed into user_stats, right before we
        rebuild sessions from the live gateway state."""
        await self.conn.execute("DELETE FROM active_sessions")
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # weekly_snapshots
    # ------------------------------------------------------------------ #

    async def take_weekly_snapshot(
        self, guild_id: int, week_start_ts: float
    ) -> None:
        """Overwrite the weekly snapshot for all tracked users in a guild
        with their current cumulative totals. Called at the start of each
        weekly reset so the next week's deltas start from zero."""
        await self.conn.execute(
            """
            INSERT INTO weekly_snapshots
                (guild_id, user_id, snap_voice, snap_streaming,
                 snap_messages, week_start_ts)
            SELECT guild_id, user_id, voice_seconds, streaming_seconds,
                   message_count, ?
            FROM user_stats WHERE guild_id = ?
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                snap_voice     = excluded.snap_voice,
                snap_streaming = excluded.snap_streaming,
                snap_messages  = excluded.snap_messages,
                week_start_ts  = excluded.week_start_ts
            """,
            (week_start_ts, guild_id),
        )
        await self.conn.commit()

    async def get_weekly_deltas(
        self, guild_id: int
    ) -> list[tuple[int, float, float, int]]:
        """Return (user_id, voice_delta, streaming_delta, msg_delta) for
        every tracked user in the guild. Delta = current total - snapshot
        taken at the start of the current week. Users with no snapshot yet
        have their all-time total treated as the delta."""
        cur = await self.conn.execute(
            """
            SELECT
                u.user_id,
                MAX(0.0, u.voice_seconds     - COALESCE(w.snap_voice,     0)) AS voice_delta,
                MAX(0.0, u.streaming_seconds - COALESCE(w.snap_streaming, 0)) AS stream_delta,
                MAX(0,   u.message_count     - COALESCE(w.snap_messages,  0)) AS msg_delta
            FROM user_stats u
            LEFT JOIN weekly_snapshots w
                   ON u.guild_id = w.guild_id AND u.user_id = w.user_id
            WHERE u.guild_id = ?
            """,
            (guild_id,),
        )
        rows = await cur.fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    # ------------------------------------------------------------------ #
    # daily_snapshots  (n-day activity windows)
    # ------------------------------------------------------------------ #

    async def take_daily_snapshot(
        self, guild_id: int, now_ts: float
    ) -> None:
        """Take a snapshot for today (UTC midnight). Idempotent: a second
        call for the same UTC day is a no-op. Prunes snapshots older than
        31 days so the table stays bounded."""
        today = datetime.datetime.fromtimestamp(
            now_ts, tz=datetime.timezone.utc
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        today_ts = today.timestamp()

        await self.conn.execute(
            """
            INSERT OR IGNORE INTO daily_snapshots
                (guild_id, user_id, snapshot_date,
                 snap_voice, snap_streaming, snap_messages)
            SELECT guild_id, user_id, ?,
                   voice_seconds, streaming_seconds, message_count
            FROM user_stats WHERE guild_id = ?
            """,
            (today_ts, guild_id),
        )
        cutoff = (today - datetime.timedelta(days=31)).timestamp()
        await self.conn.execute(
            "DELETE FROM daily_snapshots WHERE guild_id = ? AND snapshot_date < ?",
            (guild_id, cutoff),
        )
        await self.conn.commit()

    async def get_nday_deltas(
        self, guild_id: int, user_id: int, days: int
    ) -> dict[str, float | int]:
        """Return activity deltas for the last N days for one user.

        Finds the daily snapshot closest to N days ago and subtracts it
        from the current totals. If no historical snapshot exists (e.g. the
        bot was just installed), the all-time totals are returned as the
        best available approximation."""
        target = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=days
        )
        target_ts = target.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        cur = await self.conn.execute(
            """
            SELECT snap_voice, snap_streaming, snap_messages
            FROM daily_snapshots
            WHERE guild_id = ? AND user_id = ?
            ORDER BY ABS(snapshot_date - ?) ASC
            LIMIT 1
            """,
            (guild_id, user_id, target_ts),
        )
        snap = await cur.fetchone()
        stats = await self.get_user_stats(guild_id, user_id)

        if snap is None:
            return {
                "voice": stats.voice_seconds,
                "streaming": stats.streaming_seconds,
                "messages": stats.message_count,
            }
        return {
            "voice":    max(0.0, stats.voice_seconds    - snap[0]),
            "streaming": max(0.0, stats.streaming_seconds - snap[1]),
            "messages": max(0,   stats.message_count     - snap[2]),
        }

    # ------------------------------------------------------------------ #
    # guild_config
    # ------------------------------------------------------------------ #

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        """Return the config for a guild, inserting a default row if none
        exists yet."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
            (guild_id,),
        )
        await self.conn.commit()
        cur = await self.conn.execute(
            """
            SELECT anti_spam_enabled, anti_farm_enabled,
                   announcements_enabled, announcement_channel_id,
                   vote_channel_id, anti_spam_msg_limit,
                   anti_spam_window_seconds, last_weekly_reset_ts
            FROM guild_config WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = await cur.fetchone()
        return GuildConfig(
            guild_id=guild_id,
            anti_spam_enabled=bool(row[0]),
            anti_farm_enabled=bool(row[1]),
            announcements_enabled=bool(row[2]),
            announcement_channel_id=row[3],
            vote_channel_id=row[4],
            anti_spam_msg_limit=row[5],
            anti_spam_window_seconds=row[6],
            last_weekly_reset_ts=row[7],
        )

    async def set_guild_config(self, guild_id: int, **kwargs) -> None:
        """Partially update one or more guild config fields.

        Only whitelisted keys are accepted to prevent SQL injection via
        column names.
        """
        if not kwargs:
            return
        _allowed = {
            "anti_spam_enabled",
            "anti_farm_enabled",
            "announcements_enabled",
            "announcement_channel_id",
            "vote_channel_id",
            "anti_spam_msg_limit",
            "anti_spam_window_seconds",
            "last_weekly_reset_ts",
        }
        for key in kwargs:
            if key not in _allowed:
                raise ValueError(f"Unknown guild_config key: {key!r}")

        # Ensure the row exists before updating
        await self.conn.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,)
        )
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [guild_id]
        await self.conn.execute(
            f"UPDATE guild_config SET {sets} WHERE guild_id = ?", values
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # anti_farm_flags
    # ------------------------------------------------------------------ #

    async def flag_anti_farm(
        self, guild_id: int, user_id: int, flagged_at: float
    ) -> None:
        """Mark a user as a VC farmer for this week. Idempotent."""
        await self.conn.execute(
            """
            INSERT INTO anti_farm_flags (guild_id, user_id, flagged_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id, flagged_at),
        )
        await self.conn.commit()

    async def unflag_anti_farm(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM anti_farm_flags WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()

    async def get_anti_farm_flagged(self, guild_id: int) -> set[int]:
        """Return the set of user_ids currently flagged as farming."""
        cur = await self.conn.execute(
            "SELECT user_id FROM anti_farm_flags WHERE guild_id = ?",
            (guild_id,),
        )
        rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def clear_anti_farm_flags(self, guild_id: int) -> None:
        """Clear all anti-farm flags for a guild. Called at weekly reset."""
        await self.conn.execute(
            "DELETE FROM anti_farm_flags WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()


def now() -> float:
    return time.time()