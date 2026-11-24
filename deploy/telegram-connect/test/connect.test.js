// The whole setup flow against a real SQLite database (Node's built-in, standing in for Cloudflare D1) and a
// fake Telegram. Run: node --test test/connect.test.js   (Node 22.13 or later)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import worker, { createSession, onUpdate, readSession } from "../src/index.js";

// A small D1 look-alike: prepare(sql).bind(...).first() / run() over node:sqlite.
function fakeD1() {
  const sqlite = new DatabaseSync(":memory:");
  sqlite.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  return {
    sqlite,
    prepare(sql) {
      let args = [];
      const statement = {
        bind(...values) {
          args = values;
          return statement;
        },
        async first() {
          return sqlite.prepare(sql).get(...args) ?? null;
        },
        async run() {
          const result = sqlite.prepare(sql).run(...args);
          return { meta: { changes: Number(result.changes) } };
        },
      };
      return statement;
    },
  };
}

function fakeTelegram({ refuseChat = false } = {}) {
  const calls = [];
  const fetcher = async (url, init) => {
    const method = url.split("/").pop();
    const payload = JSON.parse(init.body);
    calls.push({ url, method, payload });
    if (method === "sendMessage" && refuseChat) {
      return new Response(JSON.stringify({ ok: false, description: "Forbidden: bot can't initiate conversation" }), { status: 403 });
    }
    const result = method === "getManagedBotToken" ? "999:USER-BOT-TOKEN" : { message_id: calls.length };
    return new Response(JSON.stringify({ ok: true, result }));
  };
  return { calls, fetcher };
}

const ENV = () => ({ DB: fakeD1(), BOT_TOKEN: "111:MANAGER\n", BOT_USERNAME: "SarthiiAiBot", WEBHOOK_SECRET: "hook-secret\r\n" });
const NOW = 1_800_000_000;
const USER = { id: 4242, is_bot: false, first_name: "Aarav" };
const start = (text) => ({ message: { message_id: 1, chat: { id: USER.id, type: "private" }, from: USER, text } });
const made = (username) => ({ managed_bot: { user: USER, bot: { id: 999, is_bot: true, first_name: "Sarthi", username } } });

async function body(response) {
  return { http: response.status, ...(await response.json()) };
}

async function newSession(env) {
  const session = await body(await createSession(env, "1.2.3.4", NOW));
  const code = session.fallback_link.split("c_")[1];
  return { session, code, auth: `Bearer ${session.secret}` };
}

test("one tap: the link opens Telegram's create window; the bot is matched by its username and handed over once", async () => {
  const env = ENV();
  const tg = fakeTelegram({ refuseChat: true }); // the user never opened the manager chat
  const { session, code, auth } = await newSession(env);
  assert.equal(session.start_link, `https://t.me/newbot/SarthiiAiBot/sarthi_${code}_bot?name=Sarthi`);
  assert.equal((await body(await readSession(env, session.id, auth, NOW, tg.fetcher))).status, "pending");

  await onUpdate(env, made(`sarthi_${code}_bot`), NOW + 20, tg.fetcher);
  // No token is stored: the row holds none, and Telegram was not asked for it yet.
  assert.equal(env.DB.sqlite.prepare("SELECT token FROM sessions").get().token, null);
  assert.equal(tg.calls.filter((c) => c.method === "getManagedBotToken").length, 0);

  const ready = await body(await readSession(env, session.id, auth, NOW + 25, tg.fetcher));
  assert.equal(ready.status, "ready");
  assert.equal(ready.token, "999:USER-BOT-TOKEN");
  assert.equal(ready.owner_id, USER.id);
  assert.deepEqual(tg.calls.find((c) => c.method === "getManagedBotToken").payload, { user_id: 999 });
  assert.match(tg.calls[0].url, /\/bot111:MANAGER\/sendMessage$/); // the secret's stray line break is gone
  const again = await body(await readSession(env, session.id, auth, NOW + 26, tg.fetcher));
  assert.equal(again.token, undefined);
  assert.equal(again.status, "claimed");
});

test("a renamed bot is still matched when the user came through /start c_<code>", async () => {
  const env = ENV();
  const tg = fakeTelegram();
  const { session, code, auth } = await newSession(env);
  await onUpdate(env, start(`/start c_${code}`), NOW, tg.fetcher);
  const button = tg.calls.at(-1).payload.reply_markup.inline_keyboard[0][0];
  assert.equal(button.url, `https://t.me/newbot/SarthiiAiBot/sarthi_${code}_bot?name=Sarthi`);
  await onUpdate(env, made("aarav_jobs_bot"), NOW + 30, tg.fetcher);
  assert.equal((await body(await readSession(env, session.id, auth, NOW + 40, tg.fetcher))).bot_username, "aarav_jobs_bot");
});

test("a renamed bot from someone who never opened /start is not handed to anyone, and they are told", async () => {
  const env = ENV();
  const tg = fakeTelegram();
  const { session, auth } = await newSession(env);
  await onUpdate(env, made("aarav_jobs_bot"), NOW, tg.fetcher);
  assert.match(tg.calls.at(-1).payload.text, /could not tell which Sarthi/);
  assert.equal((await body(await readSession(env, session.id, auth, NOW, tg.fetcher))).status, "pending");
  assert.equal(tg.calls.filter((c) => c.method === "getManagedBotToken").length, 0);
});

test("another user's code in a bot's username cannot steal a session that is already done", async () => {
  const env = ENV();
  const tg = fakeTelegram();
  const { session, code, auth } = await newSession(env);
  await onUpdate(env, made(`sarthi_${code}_bot`), NOW, tg.fetcher);
  await readSession(env, session.id, auth, NOW, tg.fetcher); // claimed by its Sarthi
  const thief = { managed_bot: { user: { id: 7 }, bot: { id: 5, username: `sarthi_${code}_bot` } } };
  await onUpdate(env, thief, NOW + 1, tg.fetcher);
  const row = env.DB.sqlite.prepare("SELECT status, tg_user_id FROM sessions").get();
  assert.deepEqual({ ...row }, { status: "claimed", tg_user_id: USER.id });
});

test("the token is never given to anyone without the session's secret", async () => {
  const env = ENV();
  const { session } = await newSession(env);
  for (const auth of [null, "", "Bearer wrong", `Bearer ${session.secret}x`]) {
    assert.equal((await readSession(env, session.id, auth, NOW)).status, 404);
  }
});

test("sessions expire after 15 minutes and old links are refused", async () => {
  const env = ENV();
  const tg = fakeTelegram();
  const { session, code, auth } = await newSession(env);
  await onUpdate(env, start(`/start c_${code}`), NOW + 16 * 60, tg.fetcher);
  assert.match(tg.calls.at(-1).payload.text, /expired/);
  assert.equal((await readSession(env, session.id, auth, NOW + 16 * 60)).status, 404);
});

test("too many setups from one address are slowed down", async () => {
  const env = ENV();
  for (let i = 0; i < 10; i += 1) assert.equal((await createSession(env, "9.9.9.9", NOW)).status, 200);
  assert.equal((await createSession(env, "9.9.9.9", NOW)).status, 429);
  assert.equal((await createSession(env, "8.8.8.8", NOW)).status, 200);
  assert.equal((await createSession(env, "9.9.9.9", NOW + 3601)).status, 200);
});

test("the webhook accepts only Telegram's secret (stray line breaks ignored) and always answers 200 to it", async () => {
  const env = ENV();
  const post = (secret) =>
    worker.fetch(
      new Request("https://w.example/telegram", {
        method: "POST",
        headers: secret === null ? {} : { "x-telegram-bot-api-secret-token": secret },
        body: JSON.stringify({ update_id: 1 }),
      }),
      env,
    );
  assert.equal((await post(null)).status, 403);
  assert.equal((await post("")).status, 403);
  assert.equal((await post("guess")).status, 403);
  assert.equal((await post("hook-secret")).status, 200); // the stored secret ended in "\r\n"
  assert.equal((await worker.fetch(new Request("https://w.example/healthz"), env)).status, 200);
  assert.equal((await worker.fetch(new Request("https://w.example/v1/sessions/../../etc"), env)).status, 404);
});
