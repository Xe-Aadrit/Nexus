"""
Message counting.

`on_message` fires for every message the bot can see: normal text channels,
threads, and the built-in text chat attached to voice channels - no extra
intent or permission is needed beyond being able to view the channel, and we
deliberately never enable the privileged `message_content` intent since we
only need to *count* messages, not read them.

We only count genuine user messages (regular messages and replies), not
system messages such as "X joined the thread" or pin notifications, and we
ignore DMs (this bot only tracks guild activity) and other bots.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from database import Database

log = logging.getLogger(__name__)

COUNTABLE_TYPES = {
    discord.MessageType.default,
    discord.MessageType.reply,
}


class MessageTracking(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return  # ignore DMs
        if message.author.bot:
            return
        if message.type not in COUNTABLE_TYPES:
            return

        async with self.db.lock:
            await self.db.add_deltas(
                message.guild.id, message.author.id, messages=1
            )