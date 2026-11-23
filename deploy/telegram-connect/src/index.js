// Sarthi's Telegram connector: a Cloudflare Worker that gives every Sarthi user their own Telegram bot in
// one tap, with no @BotFather and no token to copy (Telegram "managed bots", Bot API 9.6).
//
// It runs only when called (no server to keep on) and holds nothing for long:
//   1. A Sarthi asks for a setup session (POST /v1/sessions) and shows the user a link to the manager bot.
//   2. The user opens it (/start c_<code>): the session is tied to their Telegram account, and the manager
//      bot shows a "Create my Sarthi bot" button.
//   3. The user taps it and confirms: Telegram creates the user's own bot and tells this worker
//      (update "managed_bot"); the worker fetches that bot's token (getManagedBotToken).
//   4. The Sarthi collects the token once (GET /v1/sessions/<id>); it is erased at once, and every session is
//      deleted after 15 minutes. From then on that Sarthi talks to Telegram directly, through its own bot.
// Messages, jobs and resumes never pass through here.

const SESSION_TTL_S = 15 * 60;
const SESSIONS_PER_IP_PER_HOUR = 10;
const CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"; // no 0/o, 1/l/i: easy to read back
const API = "https://api.telegram.org";

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

function randomText(length, alphabet = CODE_ALPHABET) {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function sameText(a, b) {
  // Constant-time comparison of two strings of equal length (hex digests here).
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

// ---- Telegram ----

export async function telegram(env, method, payload, fetcher = fetch) {
  const response = await fetcher(`${env.TELEGRAM_API || API}/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!body.ok) throw new Error(`telegram ${method}: ${String(body.description || response.status).slice(0, 200)}`);
  return body.result;
}

function say(env, chatId, text, extra = {}, fetcher = fetch) {
  return telegram(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", ...extra }, fetcher);
}

// ---- sessions (for Sarthi) ----

async function cleanUp(db, now) {
  await db.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(now).run();
  await db.prepare("DELETE FROM hits WHERE at < ?").bind(now - 3600).run();
}

export async function createSession(env, ip, now = nowSeconds()) {
  const db = env.DB;
  await cleanUp(db, now);
  const recent = await db.prepare("SELECT count(*) AS n FROM hits WHERE ip = ? AND at >= ?").bind(ip, now - 3600).first();
  if ((recent?.n ?? 0) >= SESSIONS_PER_IP_PER_HOUR) return json({ ok: false, error: "too many setups from here; try again in an hour" }, 429);
  await db.prepare("INSERT INTO hits (ip, at) VALUES (?, ?)").bind(ip, now).run();
  const id = randomText(24);
  const secret = randomText(40);
  const code = randomText(10);
  await db
    .prepare("INSERT INTO sessions (id, secret_hash, code, status, created_at, expires_at) VALUES (?, ?, ?, 'pending', ?, ?)")
    .bind(id, await sha256(secret), code, now, now + SESSION_TTL_S)
    .run();
  return json({
    ok: true,
    id,
    secret,
    start_link: `https://t.me/${env.BOT_USERNAME}?start=c_${code}`,
    expires_in: SESSION_TTL_S,
  });
}

export async function readSession(env, id, authorization, now = nowSeconds()) {
  const db = env.DB;
  const row = await db.prepare("SELECT * FROM sessions WHERE id = ? AND expires_at >= ?").bind(id, now).first();
  const given = (authorization || "").replace(/^Bearer\s+/i, "");
  if (!row || !given || !sameText(row.secret_hash, await sha256(given))) return json({ ok: false, error: "unknown or expired session" }, 404);
  if (row.status !== "ready") return json({ ok: true, status: row.status });
  // Handed over exactly once: the token is erased in the same step it is read.
  const claimed = await db.prepare("UPDATE sessions SET status = 'claimed', token = NULL WHERE id = ? AND status = 'ready'").bind(id).run();
  if (!claimed.meta || claimed.meta.changes !== 1) return json({ ok: true, status: "claimed" });
  return json({
    ok: true,
    status: "ready",
    token: row.token,
    bot_username: row.bot_username,
    bot_id: row.bot_id,
    owner_id: row.tg_user_id,
  });
}

// ---- the manager bot's updates (from Telegram) ----

function createButton(code) {
  return {
    keyboard: [[{
      text: "Create my Sarthi bot",
      request_managed_bot: { request_id: 1, suggested_name: "Sarthi", suggested_username: `sarthi_${code}_bot` },
    }]],
    resize_keyboard: true,
    one_time_keyboard: true,
  };
}

const HELP =
  "Hi! I set up Sarthi bots. Open <b>Connect Telegram</b> on your Sarthi dashboard " +
  "(http://127.0.0.1:8765) and tap the link there; it brings you back here with your setup code.";

export async function onUpdate(env, update, now = nowSeconds(), fetcher = fetch) {
  const db = env.DB;
  const message = update.message;
  if (message && message.chat && message.chat.type === "private" && typeof message.text === "string") {
    const [command, argument = ""] = message.text.trim().split(/\s+/, 2);
    if (command.split("@")[0] !== "/start") return say(env, message.chat.id, HELP, {}, fetcher);
    const code = argument.startsWith("c_") ? argument.slice(2) : "";
    const row = code
      ? await db.prepare("SELECT id FROM sessions WHERE code = ? AND status IN ('pending', 'paired') AND expires_at >= ?").bind(code, now).first()
      : null;
    if (!row) return say(env, message.chat.id, code ? "That setup link has expired. Open Connect Telegram on your Sarthi dashboard again." : HELP, {}, fetcher);
    await db.prepare("UPDATE sessions SET status = 'paired', tg_user_id = ? WHERE id = ?").bind(message.from.id, row.id).run();
    return say(
      env,
      message.chat.id,
      "Tap <b>Create my Sarthi bot</b> below, then confirm. It becomes <b>your own</b> bot: only you can talk to it, " +
        "and it talks only to the Sarthi on your computer.",
      { reply_markup: createButton(code) },
      fetcher,
    );
  }
  const managed = update.managed_bot;
  if (managed && managed.user && managed.bot) {
    const row = await db
      .prepare("SELECT id FROM sessions WHERE tg_user_id = ? AND status = 'paired' AND expires_at >= ? ORDER BY created_at DESC LIMIT 1")
      .bind(managed.user.id, now)
      .first();
    if (!row) return null; // a token change or a bot made without a setup link: nothing to hand over
    const token = await telegram(env, "getManagedBotToken", { user_id: managed.bot.id }, fetcher);
    await db
      .prepare("UPDATE sessions SET status = 'ready', token = ?, bot_id = ?, bot_username = ? WHERE id = ?")
      .bind(token, managed.bot.id, managed.bot.username || "", row.id)
      .run();
    return say(
      env,
      managed.user.id,
      `✅ <b>@${managed.bot.username}</b> is yours. Go back to your Sarthi: it connects in a few seconds and then asks you to tap Start in your new bot.`,
      { reply_markup: { remove_keyboard: true } },
      fetcher,
    );
  }
  return null;
}

// ---- routing ----

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/healthz") return json({ ok: true });
    if (request.method === "POST" && url.pathname === "/v1/sessions") {
      return createSession(env, request.headers.get("cf-connecting-ip") || "unknown");
    }
    const session = url.pathname.match(/^\/v1\/sessions\/([a-z0-9]{24})$/);
    if (request.method === "GET" && session) return readSession(env, session[1], request.headers.get("authorization"));
    if (request.method === "POST" && url.pathname === "/telegram") {
      // Only Telegram knows this secret (set with setWebhook): nobody else can post fake updates.
      if (!sameText(request.headers.get("x-telegram-bot-api-secret-token") || "", env.WEBHOOK_SECRET || "-")) {
        return new Response("forbidden", { status: 403 });
      }
      try {
        await onUpdate(env, await request.json());
      } catch (error) {
        console.log(`update not handled: ${String(error && error.message).slice(0, 200)}`);
      }
      return new Response("ok"); // always 200: Telegram would otherwise resend the same update for hours
    }
    return json({ ok: false, error: "not found" }, 404);
  },
};
