# Sarthi (सारथि)

**Your guide to the next job.** Sarthi watches 150+ job sources all day, finds jobs that fit you,
messages you on Telegram, applies when you say yes, and tells you when companies reply.

Free and open source. Runs on your own computer. Works with a free local AI (Ollama) or your own API key.

> Status: under active development (towards the first free release).

## Run with Docker

```bash
cp .env.example .env          # add one AI key (a free Gemini or Groq key is enough)
docker compose up -d          # starts Sarthi
```

Then open http://127.0.0.1:8765 and tap **Connect Telegram**: it opens the shared Sarthi bot, and after
you tap Start it messages you there. No bot to create, no code to type. (Want a bot of your own instead?
Put its token in `SARTHI_TELEGRAM_BOT_TOKEN`; see [docs/RELAY.md](docs/RELAY.md) for how the shared bot
keeps every user's chat separate.)

Everything that is yours (database, resume, documents, logins) stays in `./data` on your computer.
The dashboard is read-only and reachable from this computer only.
A local AI model is optional: `docker compose --profile ollama up -d` (needs about 2 GB of free memory).
To apply on LinkedIn or Naukri with your account, send `/login` to the bot once: it gives you a link that
shows the site's login page in your browser (http://127.0.0.1:6080). You type your password into the
site itself; Sarthi keeps only the site's session, in `./data`.

Auto-apply is off unless you turn it on: send `/autoapply 85` and Sarthi applies to matches of 85% or
more by itself, after telling you and waiting two hours for a Cancel. It sends a form without your tap
only when every answer came from your profile, never on job boards such as LinkedIn or Naukri, and at
most 10 a day. `/autoapply off` turns it off.

## Run without Docker

```bash
uv sync && uv run playwright install chromium
cp .env.example .env          # then fill it in
uv run sarthi pair            # link your Telegram chat
uv run sarthi run             # dashboard: http://127.0.0.1:8765
```

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
```

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Coding & security standards: [docs/STANDARDS.md](docs/STANDARDS.md)
- Reference repo audit: [docs/AUDIT.md](docs/AUDIT.md)

## Licence

AGPL-3.0-only. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
