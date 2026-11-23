// The whole setup flow against a real SQLite database (Node's built-in, standing in for Cloudflare D1) and a
// fake Telegram. Run: node --test test/   (Node 22.13 or later)
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

function fakeTelegram() {
  const calls = [];
  const fetcher = async (url, init) => {
    const method = url.split("/").pop();
    const payload = JSON.parse(init.body);
    calls.push({ url, method, payload });
    const result = method === "getManagedBotToken" ? "999:USER-BOT-TOKEN" : { message_id: calls.length };
    return new Response(JSON.stringify({ ok: true, result }));
  };
  return { calls, fetcher };
}

const ENV = () => ({ DB: fakeD1(), BOT_TOKEN: "111:MANAGER", BOT_USERNAME: "SarthiiAiBot", WEBHOOK_SECRET: "hook-secret" });
const NOW = 1_800_000_000;
const USER = { id: 4242, is_bot: false, first_name: "Aarav" };
const start = (text) => ({ message: { message_id: 1, chat: { id: USER.id, type: "private" }, from: USER, text } });

async function body(response) {
  return { status: response.status, ...(await response.json()) };
}

test("one tap: session, /start with the code, create button, managed bot, token handed over once", async () => {
  const env = ENV();
  const tg = fakeTelegram();
  const session = await body(await createSession(env, "1.2.3.4", NOW));
  assert.equal(session.ok, true);
  assert.match(session.start_link, /^https:\/\/t\.me\/SarthiiAiBot\?start=c_[a-z2-9]{10}$/);
  const code = session.start_link.split("c_")[1];

  // Not ready yet.
  assert.deepEqual(await body(await readSession(env, session.id, `Bearer ${session.secret}`, NOW)), { status: 200, ok: true, status: "pending" });

  await onUpdate(env, start(`/start c_${code}`), NOW, tg.fetcher);
  const button = tg.calls.at(-1).payload.reply_markup.keyboard[0][0];
  assert.equal(button.text, "Create my Sarthi bot");
  assert.deepEqual(button.request_managed_bot, { request_id: 1, suggested_name: "Sarthi", suggested_username: `sarthi_${code}_bot` });

  // The user renamed the bot in Telegram's window: still matched, by who created it.
  const created = { managed_bot: { user: USER, bot: { id: 999, is_bot: true, first_name: "Sarthi", username: "aarav_jobs_bot" } } };
  await onUpdate(env, created, NOW + 30, tg.fetcher);
  assert.deepEqual(tg.calls.find((c) => c.method === "getManagedBotToken").payload, { user_id: 999 });

  const ready = await body(await readSession(env, session.id, `Bearer ${session.secret}`, NOW + 40));
  assert.equal(ready.status, "ready");
  assert.equal(ready.token, "999:USER-BOT-TOKEN");
  assert.equal(ready.bot_username, "aarav_jobs_bot");
  assert.equal(ready.owner_id, USER.id);
  // Erased after the first read.
  const again = await body(await readSession(env, session.id, `Bearer ${session.secret}`, NOW + 41));
  assert.equal(again.token, undefined);
  assert.equal(again.status, "claimed");
});

test("the token is never given to anyone without the session's secret", async () => {
  const env = ENV();
  const session = await body(await createSession(env, "1.2.3.4", NOW));
  for (const auth of [null, "", "Bearer wrong", `Bearer ${session.secret}x`]) {
    assert.equal((await readSession(env, session.id, auth, NOW)).status, 404);
  }
});

test("sessions expire after 15 minutes and old links are refused", async () => {
  const env = ENV();
  const tg = fakeTelegram();
  const session = await body(await createSession(env, "1.2.3.4", NOW));
  const code = session.start_link.split("c_")[1];
  await onUpdate(env, start(`/start c_${code}`), NOW + 16 * 60, tg.fetcher);
  assert.match(tg.calls.at(-1).payload.text, /expired/);
  assert.equal((await readSession(env, session.id, `Bearer ${session.secret}`, NOW + 16 * 60)).status, 404);
});

test("a bot created by someone who never opened a setup link is not handed to anyone", async () => {
  const env = ENV();
  const tg = fakeTelegram();
  await createSession(env, "1.2.3.4", NOW);
  const stranger = { managed_bot: { user: { id: 7 }, bot: { id: 5, username: "x_bot" } } };
  assert.equal(await onUpdate(env, stranger, NOW, tg.fetcher), null);
  assert.equal(tg.calls.length, 0); // no token was even fetched
});

test("too many setups from one address are slowed down", async () => {
  const env = ENV();
  for (let i = 0; i < 10; i += 1) assert.equal((await createSession(env, "9.9.9.9", NOW)).status, 200);
  assert.equal((await createSession(env, "9.9.9.9", NOW)).status, 429);
  assert.equal((await createSession(env, "8.8.8.8", NOW)).status, 200);
  assert.equal((await createSession(env, "9.9.9.9", NOW + 3601)).status, 200);
});

test("the webhook accepts only Telegram (its secret header) and always answers 200 to it", async () => {
  const env = ENV();
  const post = (secret) =>
    worker.fetch(
      new Request("https://w.example/telegram", {
        method: "POST",
        headers: secret ? { "x-telegram-bot-api-secret-token": secret } : {},
        body: JSON.stringify({ update_id: 1 }),
      }),
      env,
    );
  assert.equal((await post(null)).status, 403);
  assert.equal((await post("guess")).status, 403);
  assert.equal((await post("hook-secret")).status, 200);
  const health = await worker.fetch(new Request("https://w.example/healthz"), env);
  assert.equal(health.status, 200);
  const unknown = await worker.fetch(new Request("https://w.example/v1/sessions/../../etc"), env);
  assert.equal(unknown.status, 404);
});
