# Sarthi connector (one-tap Telegram setup)

Gives every Sarthi user a Telegram bot of their own in one tap, with no @BotFather and no token to copy. It
uses Telegram's [managed bots](https://core.telegram.org/bots/features#managed-bots) (Bot API 9.6) and runs
as a [Cloudflare Worker](https://developers.cloudflare.com/workers/): nothing to keep switched on, and the
free plan is plenty (a few requests per new user, once).

**What it sees:** only the setup handshake. It never stores a bot token: it asks Telegram for it at the moment
the Sarthi that asked collects it, once, and deletes every session after 15 minutes. Messages, jobs and
resumes never pass through it; after setup each Sarthi talks to Telegram directly.

**What the user does:** taps *Create my Sarthi bot* on the dashboard; Telegram opens its own "Create bot"
window with the name filled in; they tap Create, then Start in the new bot. Nothing to type.

**What users trust:** Telegram lets a manager bot fetch or reset the tokens of the bots made through it. This
code never does so after the hand-over, and it is open for anyone to check. People who want no manager at all
can still use a bot of their own from @BotFather (`SARTHI_TELEGRAM_BOT_TOKEN`).

## Deploy (once, about 10 minutes)

You need [Node.js](https://nodejs.org) 22.13 or later and a free [Cloudflare](https://dash.cloudflare.com/sign-up)
account. Run these from this folder (`deploy/telegram-connect`).

> **On Windows:** use a normal folder (cmd cannot `cd` into a `\\wsl.localhost\...` path), for example a copy of
> this folder in your user folder, and install Wrangler there: `npm install --save-dev wrangler`. If it then says
> *"@cloudflare/workerd-windows-64 could not be found"* (an npm bug with optional packages), install the version
> it names: `npm install --save-dev @cloudflare/workerd-windows-64@<version>`. Type secrets at Wrangler's prompt
> rather than piping them from PowerShell, which adds an invisible line break (the worker trims it anyway).

1. **Log in to Cloudflare** (opens your browser):

   ```bash
   npx wrangler login
   ```

2. **Create the database** and put the `database_id` it prints into `wrangler.toml`:

   ```bash
   npx wrangler d1 create sarthi-connect
   npx wrangler d1 execute sarthi-connect --remote --file=schema.sql
   ```

3. **Add the two secrets.** `BOT_TOKEN` is the manager bot's token from @BotFather (for Sarthi:
   @SarthiiAiBot). `WEBHOOK_SECRET` is any long random text; Telegram sends it with every update so nobody
   else can post fake ones.

   ```bash
   npx wrangler secret put BOT_TOKEN
   npx wrangler secret put WEBHOOK_SECRET
   ```

4. **Deploy.** The first time, Cloudflare asks for a `workers.dev` name for your account (it becomes part of the
   address). It prints the address, like `https://sarthi-connect.<your-name>.workers.dev`:

   ```bash
   npx wrangler deploy
   ```

5. **Turn on Bot Management Mode** for the manager bot: open `https://t.me/BotFather?startapp`, pick the bot,
   and switch on *Bot Management Mode*.

6. **Point the manager bot at the worker.** Nothing else may be reading this bot's updates (stop a shared-bot
   relay first: Telegram allows a webhook or polling, not both). Replace the three values:

   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=https://sarthi-connect.<your-name>.workers.dev/telegram" \
     -d "secret_token=<WEBHOOK_SECRET>" \
     -d 'allowed_updates=["message","managed_bot"]'
   ```

7. **Check it:** `https://sarthi-connect.<your-name>.workers.dev/healthz` answers `{"ok":true}`, and sending
   `/start` to the manager bot gets a reply.

Users then set `SARTHI_TELEGRAM_CONNECT_URL=https://sarthi-connect.<your-name>.workers.dev` (Sarthi's
default once the project's connector is live) and tap **Connect Telegram** on their dashboard.

## Test

```bash
node --test test/connect.test.js
```

The tests run the whole flow on a real SQLite database (Node's own) in place of Cloudflare D1, with Telegram
faked: one-tap setup, a renamed bot, the token handed over exactly once, expiry, the rate limit and the
webhook secret.
