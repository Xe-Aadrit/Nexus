# Discord Activity Tracking Bot

Tracks, per user per server: time in voice channels, time with an effectively
unmuted mic, real speaking time (optional, see below), time spent
screen-sharing, and number of text messages sent (regular channels, threads,
and the text chat attached to voice channels). All stats persist in a SQLite
database and survive restarts.

## Project layout

```
bot.py                      entry point: intents, cog wiring, graceful shutdown
config.py                   env-var based configuration
database.py                 aiosqlite persistence layer
cogs/voice_tracking.py      voice time / mute time / stream time (core)
cogs/message_tracking.py    message counting
cogs/speaking_tracking.py   optional: real speaking-time via voice receive
cogs/stats.py               !stats and !activityboard commands
requirements.txt
.env.example
```

## Setup

1. **Create the bot application**
   - Go to the [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot.
   - Under "Bot", copy the token (you'll put this in `.env`).
   - Under "Privileged Gateway Intents", leave everything **off**. This bot
     does not need Presence, Server Members, or Message Content.

2. **Invite the bot with minimal permissions**
   - Under OAuth2 → URL Generator, scope: `bot`.
   - Permissions to tick: `View Channels`, `Send Messages`,
     `Embed Links`, `Read Message History`, `Connect`.
     (`Connect` is only needed at all if you plan to enable optional
     speaking-time tracking, see below - otherwise you can leave it out too.)
   - **Do not** grant `Administrator`. Nothing in this bot needs it.
   - Use the generated URL to invite the bot to your server.

3. **Install dependencies**
   ```bash
   python -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. **Configure**
   ```bash
   cp .env.example .env
   # edit .env and set DISCORD_TOKEN
   ```

5. **Run**
   ```bash
   python bot.py
   ```
   The SQLite file (`activity_stats.sqlite3` by default) is created
   automatically on first run in the working directory.

## Commands

- `!stats [@member]` - voice/mic/speaking/streaming time and message count for
  yourself or another member.
- `!activityboard <voice|unmuted|speaking|streaming|messages>` - top 10 for a
  metric in the current server.

(Change the prefix with `COMMAND_PREFIX` in `.env`. Slash commands would work
just as well here - this uses a plain prefix command for simplicity; porting
`cogs/stats.py` to `app_commands` is a small, self-contained change if you'd
rather have `/stats`.)

## Permissions and intents actually used

**Gateway intents:** `guilds`, `voice_states`, `guild_messages`. None of
these are privileged. The bot deliberately does **not** request
`message_content` (we only count messages, never read them), `members`, or
`presences` - the events we listen to already carry full `Member` objects.

**Bot permissions:** `View Channels`, `Send Messages`, `Embed Links`,
`Read Message History` (for the stats/leaderboard commands), and `Connect`
(only relevant if you turn on optional speaking-time tracking - see below).
No `Administrator`, no channel-management permissions, no moderation
permissions.

## How each metric is tracked

- **Voice time / mic-unmuted time / streaming time**: derived entirely from
  `on_voice_state_update` events (channel join/leave/move, `self_mute`,
  server `mute`, `self_stream`). No voice connection is needed for these
  three. "Unmuted" means neither self-muted nor server-muted - if a mod
  server-mutes someone, their mic can't transmit regardless of their own
  toggle, so that time is correctly excluded.
- **Messages**: `on_message`, counted (not read) for every channel type the
  bot can see, including threads and voice-channel text chat. System
  messages (pins, join notices, etc.) are excluded.
- **Speaking time** (optional, off by default): see below.

## Optional: real speaking-time detection

Mic-unmuted time is not the same as talking - someone can sit unmuted in
silence indefinitely. Getting *actual* speaking time requires the bot to
join the voice channel and read Discord's voice-activity signal (the same
one that lights up the green ring around an avatar). The regular gateway
does not expose this; it's only available on the voice connection itself.

This is implemented as a separate, opt-in cog
(`cogs/speaking_tracking.py`) using the third-party
[`discord-ext-voice-recv`](https://github.com/imayhaveborkedit/discord-ext-voice-recv)
extension, which is **not** part of discord.py and is explicitly
experimental. It listens for the library's synthesized speaking
start/stop events; audio itself is **discarded the instant it's received**
in `write()` - nothing is decoded to a playable format, buffered, written to
disk, or inspected. Only speaking-state timestamps are kept.

To enable it:

```bash
pip install discord-ext-voice-recv
# plus ffmpeg and libopus on the host, e.g.:
#   Debian/Ubuntu: sudo apt install ffmpeg libopus0
#   macOS:         brew install ffmpeg opus
```
Then set `ENABLE_SPEAKING_TRACKING=true` in `.env`. The bot needs the
`Connect` permission in the relevant channels. If the optional package isn't
installed, the bot logs a warning and continues without this feature - it's
never required for the rest of the bot to work.

**Read this before enabling it:**

1. A bot can only be connected to **one voice channel per guild at a time**
   - that's a hard Discord limitation. If a server has several active voice
     channels simultaneously, the bot auto-joins whichever one currently has
     the most people and follows activity around as best it can
     (`SPEAKING_MIN_MEMBERS` / `SPEAKING_IDLE_TIMEOUT_SECONDS` tune this).
     Channels it isn't currently in still get accurate voice/mute/stream
     time, just not `speaking_seconds` for that window.
2. This pulls in extra native dependencies and an unofficial library, so
   treat it as best-effort rather than production-hardened.
3. A bot joining a channel to measure who's talking - even though it never
   records audio - can reasonably be seen as a privacy-sensitive behavior by
   your members. Tell them it's happening.

If this tradeoff isn't worth it for your use case, just leave the feature
off; everything else works fully without it.

## Correctness notes

- **Restarts / crashes**: in-progress voice/mute/stream/speaking timers live
  in memory and are flushed to the database every `FLUSH_INTERVAL_SECONDS`
  (default 30), which both persists the accumulated time and rolls the
  in-memory start time forward - so nothing is ever double-counted. This
  bounds data loss from a hard crash (`kill -9`, power loss) to at most one
  flush interval per active user. On an orderly shutdown (Ctrl+C, or
  `SIGTERM` from Docker/systemd) the bot does one final flush before exiting,
  so normal restarts lose nothing. On startup, any leftover session
  checkpoint from an unclean shutdown is discarded (not resumed against the
  current time - we have no reliable way to know what really happened while
  the process was down) and sessions are rebuilt from whatever the gateway
  reports as the live voice state.
- **Simultaneous events**: Discord can deliver a channel move and a mute
  toggle in a single `voice_state_update` payload; the handler diffs
  `before`/`after` across channel, mute, and stream state together and
  applies them atomically under a per-user `asyncio.Lock`, so nothing is
  lost or double-counted regardless of how many things changed at once.
- **Precision**: durations are stored as floating-point seconds and only
  rounded when displayed, not on every individual flush - otherwise many
  small sub-second updates (e.g. someone rapidly toggling mute) would each
  round down to zero and the bot would silently under-count.
- **No audio is ever stored**, with or without speaking-time tracking
  enabled - the base bot never even opens a voice connection, and the
  optional speech-detection sink discards every audio packet immediately
  after using it to derive a boolean speaking state.

## Extending this

A few things intentionally left out to keep the scope focused, listed here
in case you want them:

- Slash-command versions of `/stats` and `/activityboard`.
- Backfilling message counts from channel history on first join (would need
  `Read Message History` more heavily and is rate-limit-sensitive).
- Per-channel (rather than per-guild-total) voice/message breakdowns - the
  schema would just need a `channel_id` column and a less aggregated query.
- Tracking webcam-on time separately from screen-share (`self_video` vs.
  `self_stream` on `VoiceState`).