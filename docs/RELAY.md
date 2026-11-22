# The shared Sarthi bot (relay)

Users do not create a Telegram bot. Every Sarthi talks to **one** shared bot through a small relay server
that the project runs. The relay holds the bot token; each user's Sarthi has only its own secret.

## How a user connects

1. `docker compose up -d` (with `SARTHI_RELAY_URL` set; the released image will carry the project's address).
2. Sarthi registers with the relay once and keeps its secret in `data/relay.json` (readable by the user only).
3. The dashboard (and the log) shows **Connect Telegram**: `https://t.me/<bot>?start=<one-time code>`.
4. The user opens it and taps Start. The relay binds that chat to that Sarthi; Sarthi checks the code too.

A new link appears every 15 minutes until one is used. A code works once.

## What the relay guarantees (tested in `tests/relay/test_relay.py`)

- A Sarthi gets updates only from its own chat, and can send only to its own chat, whatever `chat_id` it names.
- It can download only files its chat sent, and answer only button taps from its chat.
- Only the methods Sarthi uses are passed on (`sendMessage`, `editMessageReplyMarkup`, `sendDocument`,
  `sendPhoto`, `answerCallbackQuery`, `getFile`); everything else is refused. The command menu is the relay's.
- Pacing: 40 messages a minute and 2,000 a day per Sarthi, 25 a second for the whole bot (Telegram's limit is
  about 30), 10 sign-ups an hour per address.
- Groups are ignored. Strangers are told once an hour how to start.
- Updates wait up to a week for a Sarthi that is offline. Never-paired sign-ups are forgotten after a day.
- Only hashes of secrets and codes are stored. Message text is never logged and is deleted once fetched.

Privacy note: message text passes through the relay (as it passes through Telegram). Users who do not want
that can use a bot of their own (`SARTHI_TELEGRAM_BOT_TOKEN`), which bypasses the relay entirely.

## Running the relay (project operators)

```bash
SARTHI_RELAY_BOT_TOKEN=<the shared bot token> SARTHI_DATA_DIR=/var/lib/sarthi-relay sarthi relay
```

It listens on port 8780 (`SARTHI_RELAY_PORT`) and must be served over **HTTPS** (a reverse proxy such as
Caddy). Behind a proxy set `SARTHI_RELAY_TRUST_FORWARDED=true` so sign-ups are counted per real address.
One small VM is enough to start: the relay is one process with one SQLite file. The bot token must belong
to the relay alone: a Sarthi running with the same token would take its updates (Telegram allows one reader).
