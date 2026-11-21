# Sarthi (सारथि)

**Your guide to the next job.** Sarthi watches 150+ job sources all day, finds jobs that fit you,
messages you on Telegram, applies when you say yes, and tells you when companies reply.

Free and open source. Runs on your own computer. Works with a free local AI (Ollama) or your own API key.

> Status: under active development (towards the first free release).

## Run with Docker

```bash
cp .env.example .env          # add your Telegram bot token (from @BotFather) and an AI key
docker compose up -d          # starts Sarthi; the dashboard is at http://127.0.0.1:8765
docker compose run --rm sarthi pair   # shows the code to send your bot, once
```

Everything that is yours (database, resume, documents, logins) stays in `./data` on your computer.
The dashboard is read-only and reachable from this computer only.
A local AI model is optional: `docker compose --profile ollama up -d` (needs about 2 GB of free memory).
Logging in to LinkedIn or Naukri opens a browser window, so run `sarthi login linkedin` outside Docker.

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
