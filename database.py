"""
Persistence layer.

Everything goes through a single aiosqlite connection guarded by an
asyncio.Lock. SQLite serialises writes internally anyway, so this doesn't
cost real concurrency, but it does give us clean atomic
"read-modify-write" sequences without reaching for a heavier database.

Schema
------
user_stats
    One row per (guild_id, user_id). Holds cumulative totals. This is the
    "source of truth" that survives restarts.

active_sessions
    One row per (guild_id, user_id) currently in a voice channel. Stores the
    wall-clock timestamps (epoch seconds) at which each currently-open timer
    (voice / unmuted / speaking / streaming) last started or was checkpointed.
    Rows are deleted when a user leaves voice. This table exists purely so
    that a crash doesn't lose more than one flush interval's worth of data
    (see cogs/voice_tracking.py for the flush/checkpoint logic) and so a
    clean restart can tell which sessions were still open when it went down.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_stats (
    guild_id           INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    voice_seconds      REAL NOT NULL DEFAULT 0,
    unmuted_seconds    REAL NOT NULL DEFAULT 0,
    speaking_seconds   REAL NOT NULL DEFAULT 0,
    streaming_seconds  REAL NOT NULL DEFAULT 0,
    message_count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS active_sessions (
    guild_id             INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    channel_id           INTEGER NOT NULL,
    voice_started_at     REAL NOT NULL,
    unmuted_started_at   REAL,
    speaking_started_at  REAL,
    streaming_started_at REAL,
    PRIMARY KEY (guild_id, user_id)
);
"""


@dataclass
class UserStats:
    guild_id: int
    user_id: int
    voice_seconds: float = 0.0
    unmuted_seconds: float = 0.0
    speaking_seconds: float = 0.0
    streaming_seconds: float = 0.0
    message_count: int = 0


@dataclass
class SessionRow:
    guild_id: int
    user_id: int
    channel_id: int
    voice_started_at: float
    unmuted_started_at: Optional[float]
    speaking_started_at: Optional[float]
    streaming_started_at: Optional[float]


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

    async def get_user_stats(self, guild_id: int, user_id: int) -> UserStats:
        cur = await self.conn.execute(
            """
            SELECT voice_seconds, unmuted_seconds, speaking_seconds,
                   streaming_seconds, message_count
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
        }
        if order_by not in allowed:
            raise ValueError(f"invalid order_by: {order_by}")
        cur = await self.conn.execute(
            f"""
            SELECT user_id, voice_seconds, unmuted_seconds, speaking_seconds,
                   streaming_seconds, message_count
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


def now() -> float:
    return time.time()