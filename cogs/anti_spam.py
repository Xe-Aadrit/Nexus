"""
Anti Spam system.

Prevents users from gaining message-count credit through spam — defined as
sending multiple messages within a short sliding time window.

Both the message limit and the window duration are configurable per guild by
server admins (via !spamconfig / /spamconfig). The defaults (5 messages in
5 seconds) are stored in guild_config and can be changed at any time without
restarting the bot.

When Anti Spam is enabled for a guild, MessageTracking.on_message() calls
AntiSpam.is_spam() before incrementing a user's count. If is_spam() returns
True the message is silently not counted but is otherwise processed normally
(commands still work, the message is not deleted).

Toggle: !antispam on|off  /  /antispam on|off
Configure: !spamconfig <count> <seconds>  /  /spamconfig <count> <seconds>
"""
from __future__ import annotations

import collections
import logging
import time
from typing import DefaultDict

import discord
from discord.ext import commands

from database import Database

log = logging.getLogger(__name__)


class AntiSpam(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        # Per (guild_id, user_id): a deque of recent message timestamps (UTC
        # epoch seconds). Entries outside the current window are pruned on
        # each access — the deque never grows unboundedly.
        self._windows: DefaultDict[
            tuple[int, int], collections.deque
        ] = collections.defaultdict(collections.deque)

    async def is_spam(self, message: discord.Message) -> bool:
        """Return True if this message should NOT count toward activity stats.

        Called by MessageTracking.on_message() before writing to the database.
        Must only be called from the event loop (it is never thread-safe).
        """
        if message.guild is None or message.author.bot:
            return False

        async with self.db.lock:
            cfg = await self.db.get_guild_config(message.guild.id)

        if not cfg.anti_spam_enabled:
            return False

        key = (message.guild.id, message.author.id)
        limit: int = cfg.anti_spam_msg_limit
        window: int = cfg.anti_spam_window_seconds
        now: float = time.time()

        dq = self._windows[key]

        # Evict timestamps that have fallen outside the current window
        while dq and now - dq[0] > window:
            dq.popleft()

        if len(dq) >= limit:
            # User has already hit the threshold — this message is spam.
            # We deliberately do NOT append here: we don't want a user who
            # keeps spamming to push old entries out and reset their window.
            log.debug(
                "Anti-spam: ignoring message from %s in guild %d "
                "(%d msgs within %ds window).",
                message.author,
                message.guild.id,
                len(dq) + 1,
                window,
            )
            return True

        dq.append(now)
        return False
