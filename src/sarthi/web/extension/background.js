// Talks to the user's own Sarthi (http://127.0.0.1:8765 by default) and fills the open tab's form.
// It never presses Submit: the user reviews the form and sends it themselves.

const DEFAULT_BASE = "http://127.0.0.1:8765";

async function settings() {
  const { base, key } = await chrome.storage.local.get(["base", "key"]);
  return { base: (base || DEFAULT_BASE).replace(/\/+$/, ""), key: key || "" };
}

async function api(path, options = {}) {
  const { base, key } = await settings();
  let response;
  try {
    response = await fetch(base + path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(key ? { Authorization: `Bearer ${key}` } : {}),
      },
    });
  } catch (error) {
    throw new Error(`Sarthi is not reachable at ${base}. Is it running?`);
  }
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).detail;
    } catch (error) {
      detail = response.statusText;
    }
    if (response.status === 401) await chrome.storage.local.remove("key");
    throw new Error(detail || `Sarthi answered ${response.status}`);
  }
  return response;
}

async function pair(base, code) {
  await chrome.storage.local.set({ base: (base || DEFAULT_BASE).replace(/\/+$/, "") });
  await chrome.storage.local.remove("key");
  const answer = await (await api("/ext/pair", { method: "POST", body: JSON.stringify({ code }) })).json();
  await chrome.storage.local.set({ key: answer.key });
  return { paired: true };
}

async function jobs(url) {
  return (await api(`/ext/jobs?url=${encodeURIComponent(url || "")}`)).json();
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text);
}

async function fetchDocument(jobId, kind) {
  const response = await api(`/ext/file/${encodeURIComponent(jobId)}/${kind}`);
  const disposition = response.headers.get("content-disposition") || "";
  const name = (disposition.match(/filename="?([^";]+)"?/) || [])[1] || `${kind}.pdf`;
  return { name, data: toBase64(await response.arrayBuffer()) };
}

async function scan(tabId, url) {
  const config = await (await api(`/ext/scan-config?url=${encodeURIComponent(url)}`)).json();
  await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["scan.js", "fill.js"] });
  const found = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: (cfg) => (self.__sarthiScan ? self.__sarthiScan(cfg) : []),
    args: [config],
  });
  const fields = [];
  for (const frame of found) {
    for (const field of frame.result || []) fields.push({ ...field, key: `${frame.frameId}:${field.key}`, frame: frame.frameId });
  }
  return fields;
}

async function fill(tabId, jobId) {
  const tab = await chrome.tabs.get(tabId);
  const fields = await scan(tabId, tab.url);
  if (!fields.length) return { error: "No form on this page. Open the application form itself, then try again." };
  const sent = fields.map(({ frame, ...field }) => field);
  const result = await (await api("/ext/fill", { method: "POST", body: JSON.stringify({ job_id: jobId, fields: sent }) })).json();
  const files = {};
  for (const answer of Object.values(result.answers)) {
    if (answer.source === "file" && !files[answer.value]) files[answer.value] = await fetchDocument(jobId, answer.value);
  }
  const byFrame = new Map();
  for (const [key, answer] of Object.entries(result.answers)) {
    const [frame, own] = [Number(key.split(":")[0]), key.slice(key.indexOf(":") + 1)];
    if (!byFrame.has(frame)) byFrame.set(frame, {});
    byFrame.get(frame)[own] = answer;
  }
  const report = { filled: 0, kept: 0, failed: [], attached: [] };
  const labels = Object.fromEntries(fields.map((f) => [f.key, f.label]));
  for (const [frame, answers] of byFrame) {
    const [done] = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [frame] },
      func: (answers, files) => self.__sarthiFill(answers, files),
      args: [answers, files],
    });
    const out = done.result || { done: [], kept: [], failed: Object.keys(answers) };
    report.filled += out.done.length;
    report.kept += out.kept.length;
    report.failed.push(...out.failed.map((k) => labels[`${frame}:${k}`] || k));
    report.attached.push(...(out.attached || []));
  }
  const missing = result.missing.map((key) => ({ key, label: labels[key] || key }));
  await chrome.storage.session.set({ last: { tabId, jobId, missing } });
  return { ...report, missing: missing.map((m) => m.label), resume: result.resume };
}

async function applied(jobId) {
  const { last } = await chrome.storage.session.get("last");
  let given = [];
  if (last && last.jobId === jobId && last.missing.length) {
    const byFrame = new Map();
    for (const m of last.missing) {
      const frame = Number(m.key.split(":")[0]);
      if (!byFrame.has(frame)) byFrame.set(frame, []);
      byFrame.get(frame).push({ key: m.key.slice(m.key.indexOf(":") + 1), label: m.label });
    }
    for (const [frame, wanted] of byFrame) {
      try {
        const [read] = await chrome.scripting.executeScript({
          target: { tabId: last.tabId, frameIds: [frame] },
          func: (wanted) => (self.__sarthiRead ? self.__sarthiRead(wanted) : []),
          args: [wanted],
        });
        given.push(...(read.result || []));
      } catch (error) {
        // the page moved on after Submit: the answers cannot be read any more, the application still counts
      }
    }
  }
  await api("/ext/applied", { method: "POST", body: JSON.stringify({ job_id: jobId, answers: given }) });
  return { remembered: given.length };
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const work = {
    pair: () => pair(message.base, message.code),
    unpair: async () => (await chrome.storage.local.remove("key"), { paired: false }),
    status: async () => ({ paired: Boolean((await settings()).key), base: (await settings()).base }),
    jobs: () => jobs(message.url),
    fill: () => fill(message.tabId, message.jobId),
    applied: () => applied(message.jobId),
  }[message.type];
  if (!work) return false;
  work().then(reply, (error) => reply({ error: error.message || String(error) }));
  return true; // the reply comes later
});
