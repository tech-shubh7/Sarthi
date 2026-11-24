// Sarthi's Telegram connector: a Cloudflare Worker that gives every Sarthi user their own Telegram bot in
// one tap, with no @BotFather and no token to copy (Telegram "managed bots", Bot API 9.6).
//
// It runs only when called (no server to keep on) and stores no bot token at any time:
//   1. A Sarthi asks for a setup session (POST /v1/sessions) and shows the user its link.
//   2. The link opens Telegram's own "Create bot" window with the name and a username filled in; the
//      username carries the session's code (sarthi_<code>_bot). The user taps Create.
//   3. Telegram tells this worker (update "managed_bot"); the code in the new bot's username finds the
//      session. (If the user changed the username, opening /start c_<code> on the manager bot finds it.)
//   4. The Sarthi asks again (GET /v1/sessions/<id>): only then is the bot's token fetched from Telegram
//      (getManagedBotToken) and handed over, once. Every session is deleted after 15 minutes.
// From then on that Sarthi talks to Telegram directly, through its own bot. Messages, jobs and resumes
// never pass through here.

const SESSION_TTL_S = 15 * 60;
const SESSIONS_PER_IP_PER_HOUR = 10;
const CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"; // no 0/o, 1/l/i: easy to read back
const CODE_IN_USERNAME = /^sarthi_([a-z2-9]{10})_bot$/i;
const API = "https://api.telegram.org";

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

// Secrets set from a terminal can carry an invisible line break; compare and use them trimmed.
const clean = (value) => String(value || "").trim();

function randomText(length, alphabet = CODE_ALPHABET) {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function sameText(a, b) {
  // Constant-time comparison of two strings of equal length.
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length || a.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

// ---- Telegram ----

export async function telegram(env, method, payload, fetcher = fetch) {
  const response = await fetcher(`${env.TELEGRAM_API || API}/bot${clean(env.BOT_TOKEN)}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!body.ok) throw new Error(`telegram ${method}: ${String(body.description || response.status).slice(0, 200)}`);
  return body.result;
}

async function say(env, chatId, text, extra = {}, fetcher = fetch) {
  // A user who made their bot straight from the link never opened this chat: Telegram refuses, and that is fine.
  try {
    return await telegram(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", ...extra }, fetcher);
  } catch {
    return null;
  }
}

// ---- sessions (for Sarthi) ----

async function cleanUp(db, now) {
  await db.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(now).run();
  await db.prepare("DELETE FROM hits WHERE at < ?").bind(now - 3600).run();
}

export function createLink(env, code) {
  return `https://t.me/newbot/${env.BOT_USERNAME}/sarthi_${code}_bot?name=Sarthi`;
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
    start_link: createLink(env, code), // opens Telegram's "Create bot" window directly
    fallback_link: `https://t.me/${env.BOT_USERNAME}?start=c_${code}`,
    expires_in: SESSION_TTL_S,
  });
}

export async function readSession(env, id, authorization, now = nowSeconds(), fetcher = fetch) {
  const db = env.DB;
  const row = await db.prepare("SELECT * FROM sessions WHERE id = ? AND expires_at >= ?").bind(id, now).first();
  const given = (authorization || "").replace(/^Bearer\s+/i, "");
  if (!row || !given || !sameText(row.secret_hash, await sha256(given))) return json({ ok: false, error: "unknown or expired session" }, 404);
  if (row.status !== "ready") return json({ ok: true, status: row.status });
  // Handed over exactly once: claimed first, then the token is fetched from Telegram (never stored here).
  const claimed = await db.prepare("UPDATE sessions SET status = 'claimed' WHERE id = ? AND status = 'ready'").bind(id).run();
  if (!claimed.meta || claimed.meta.changes !== 1) return json({ ok: true, status: "claimed" });
  let token;
  try {
    token = await telegram(env, "getManagedBotToken", { user_id: row.bot_id }, fetcher);
  } catch (error) {
    await db.prepare("UPDATE sessions SET status = 'ready' WHERE id = ?").bind(id).run(); // try again next time
    return json({ ok: false, error: `could not fetch the bot's token: ${String(error.message).slice(0, 120)}` }, 502);
  }
  return json({
    ok: true,
    status: "ready",
    token,
    bot_username: row.bot_username,
    bot_id: row.bot_id,
    owner_id: row.tg_user_id,
  });
}

// ---- the manager bot's updates (from Telegram) ----

const HELP =
  "Hi! I make Sarthi bots. Open <b>Connect Telegram</b> on your Sarthi dashboard " +
  "(http://127.0.0.1:8765) and tap the link there.";

async function liveSession(db, column, value, now) {
  return db
    .prepare(`SELECT id, code FROM sessions WHERE ${column} = ? AND status IN ('pending', 'paired') AND expires_at >= ? ORDER BY created_at DESC LIMIT 1`)
    .bind(value, now)
    .first();
}

export async function onUpdate(env, update, now = nowSeconds(), fetcher = fetch) {
  const db = env.DB;
  const message = update.message;
  if (message && message.chat && message.chat.type === "private" && typeof message.text === "string") {
    // The fallback route: /start c_<code> ties the session to this Telegram user, then the same create link.
    const [command, argument = ""] = message.text.trim().split(/\s+/, 2);
    if (command.split("@")[0] !== "/start") return say(env, message.chat.id, HELP, {}, fetcher);
    const code = argument.startsWith("c_") ? argument.slice(2) : "";
    const row = code ? await liveSession(db, "code", code, now) : null;
    if (!row) return say(env, message.chat.id, code ? "That setup link has expired. Open Connect Telegram on your Sarthi dashboard again." : HELP, {}, fetcher);
    await db.prepare("UPDATE sessions SET status = 'paired', tg_user_id = ? WHERE id = ?").bind(message.from.id, row.id).run();
    return say(
      env,
      message.chat.id,
      "Tap <b>Create my Sarthi bot</b> and confirm in the window that opens. It becomes <b>your own</b> bot, " +
        "talking only to the Sarthi on your computer.",
      { reply_markup: { inline_keyboard: [[{ text: "Create my Sarthi bot", url: createLink(env, code) }]] } },
      fetcher,
    );
  }
  const managed = update.managed_bot;
  if (managed && managed.user && managed.bot) {
    // Which Sarthi asked? The code in the username we suggested, or else the user who opened /start c_<code>.
    const fromName = CODE_IN_USERNAME.exec(managed.bot.username || "");
    const row =
      (fromName && (await liveSession(db, "code", fromName[1].toLowerCase(), now))) ||
      (await liveSession(db, "tg_user_id", managed.user.id, now));
    if (!row) {
      return say(
        env,
        managed.user.id,
        "Your bot was made, but I could not tell which Sarthi it is for (was its username changed?). " +
          "Open <b>Connect Telegram</b> on your Sarthi dashboard again and keep the suggested username.",
        {},
        fetcher,
      );
    }
    await db
      .prepare("UPDATE sessions SET status = 'ready', tg_user_id = ?, bot_id = ?, bot_username = ? WHERE id = ?")
      .bind(managed.user.id, managed.bot.id, managed.bot.username || "", row.id)
      .run();
    return say(
      env,
      managed.user.id,
      `✅ <b>@${managed.bot.username}</b> is yours. Go back to your Sarthi: in a few seconds it shows one last link to open your bot.`,
      {},
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
    const session = url.pathname.match(/^\/v1\/sessions\/([a-z2-9]{24})$/);
    if (request.method === "GET" && session) return readSession(env, session[1], request.headers.get("authorization"));
    if (request.method === "POST" && url.pathname === "/telegram") {
      // Only Telegram knows this secret (set with setWebhook): nobody else can post fake updates.
      if (!sameText(clean(request.headers.get("x-telegram-bot-api-secret-token")), clean(env.WEBHOOK_SECRET))) {
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
