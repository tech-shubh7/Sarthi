const $ = (id) => document.getElementById(id);
const send = (message) => chrome.runtime.sendMessage(message);

function say(text) {
  $("message").textContent = text || "";
}

function item(text) {
  const li = document.createElement("li");
  li.textContent = text; // page and job text is shown as text, never as HTML
  return li;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function showPair() {
  $("main").hidden = true;
  $("pair").hidden = false;
  const status = await send({ type: "status" });
  $("base").value = status.base || "";
  $("code").focus();
}

async function showMain() {
  $("pair").hidden = true;
  $("main").hidden = false;
  const tab = await activeTab();
  const answer = await send({ type: "jobs", url: tab ? tab.url : "" });
  if (answer.error) {
    say(answer.error);
    if (/pair/i.test(answer.error)) await showPair();
    return;
  }
  const select = $("job");
  select.replaceChildren();
  for (const job of answer.jobs) {
    const option = document.createElement("option");
    option.value = job.id;
    option.textContent = `${job.title} · ${job.company}`;
    if (job.id === answer.matched) option.selected = true;
    select.append(option);
  }
  $("fill").disabled = !answer.jobs.length;
  $("job-hint").textContent = answer.matched
    ? "This page belongs to the selected job."
    : answer.jobs.length
      ? "Pick the job this form is for."
      : "No job is waiting for you in Sarthi. Open one from Applications → Apply yourself.";
}

$("pair-go").addEventListener("click", async () => {
  say("");
  $("pair-go").disabled = true;
  const answer = await send({ type: "pair", base: $("base").value.trim(), code: $("code").value.trim() });
  $("pair-go").disabled = false;
  if (answer.error) say(answer.error);
  else await showMain();
});

$("fill").addEventListener("click", async () => {
  say("");
  const tab = await activeTab();
  const jobId = $("job").value;
  $("fill").disabled = true;
  $("fill").textContent = "Filling…";
  const answer = await send({ type: "fill", tabId: tab.id, jobId });
  $("fill").disabled = false;
  $("fill").textContent = "Fill this form";
  if (answer.error) {
    say(answer.error);
    return;
  }
  const result = $("result");
  result.replaceChildren();
  const head = document.createElement("p");
  head.textContent = `Filled ${answer.filled} field(s)` +
    (answer.kept ? `, left ${answer.kept} already filled as they were` : "") + ".";
  result.append(head);
  if (answer.attached.length) result.append(item(`Attached: ${answer.attached.join(", ")}`));
  else if (!answer.resume) result.append(item("Your tailored resume for this job is being made: try again in a minute."));
  if (answer.missing.length) {
    const p = document.createElement("p");
    p.textContent = "Only you can answer these (they are saved for next time):";
    const list = document.createElement("ul");
    answer.missing.slice(0, 12).forEach((label) => list.append(item(label)));
    result.append(p, list);
  }
  if (answer.failed.length) {
    const p = document.createElement("p");
    p.textContent = "Please check these by hand:";
    const list = document.createElement("ul");
    answer.failed.slice(0, 12).forEach((label) => list.append(item(label)));
    result.append(p, list);
  }
  const done = document.createElement("p");
  done.className = "hint";
  done.textContent = "Check the form, then press the site's own Submit. Sarthi never submits for you.";
  result.append(done);
  result.hidden = false;
  $("applied").hidden = false;
});

$("applied").addEventListener("click", async () => {
  say("");
  const answer = await send({ type: "applied", jobId: $("job").value });
  if (answer.error) {
    say(answer.error);
    return;
  }
  $("applied").hidden = true;
  $("result").replaceChildren(item(
    answer.remembered ? `Recorded. ${answer.remembered} new answer(s) saved for next time.` : "Recorded as applied. Good luck!",
  ));
});

$("unpair").addEventListener("click", async (event) => {
  event.preventDefault();
  await send({ type: "unpair" });
  await showPair();
});

(async () => {
  const status = await send({ type: "status" });
  if (status.paired) await showMain();
  else await showPair();
})();
