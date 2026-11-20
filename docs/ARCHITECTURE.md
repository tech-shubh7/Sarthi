# Sarthi — Architecture (draft, 2026-11-20)

Sarthi watches every job source all day, finds jobs that fit you, messages you on Telegram,
applies when you say yes, and tells you when companies reply.

Built from 13 audited open-source repos (all repos equal weight). Rule for every part:
**if a repo already does it best → copy it. If nobody does it well → we build the better version (+1).**

Language: **Python** (most reusable code is Python). TypeScript/JS ideas are ported.
Storage: **one SQLite file**. Runs on the user's machine in **Docker**. Licence: **AGPL-3.0**.

Tags: **[MVP]** = in MVP · **[Final]** = in Final release.

---

## 1. The whole flow

```
 ┌──────────────────────────── SOURCES (150+) ────────────────────────────┐
 │ Company job systems │ Job boards │ Free job feeds │ Freehire │ Telegram │
 └────────────────────────────────┬────────────────────────────────────────┘
                                  ▼
                      SCHEDULER + TASK QUEUE  (when to check what)
                                  ▼
                 NORMALIZE → one job format (+ country codes)
                                  ▼
                 DEDUPE  (same job from 5 sites = 1 job)
                                  ▼
             QUALITY GATE  (still open? scam? repost? visa ok?)
                                  ▼
          MATCH FUNNEL  (free filter → hard rules → AI score → "why")
                                  ▼
          DECIDE  (hide / instant alert / daily digest / auto-apply)
                                  ▼
 ┌────────────── TELEGRAM BOT  ⇄  USER  (Apply · Skip · Later) ──────────────┐
 └───────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
       DOCUMENTS (tailored resume + cover letter + form answers, truth-checked)
                                     ▼
       APPLY ENGINE (rule-based → replay → AI browser → hand to user)
                                     ▼
       TRACKING (inbox replies → interview / rejection / ghosted → Telegram)
```

Everything is a task in the SQLite queue, so a crash loses nothing and each step retries on its own.

---

## 2. Each part: who does it best, what we copy, what we add

### 2.1 Sources [MVP]
| Source group | Best existing code | Copy |
|---|---|---|
| ~100 company job systems (Workday, Greenhouse, Lever, iCIMS, Taleo, SuccessFactors, Darwinbox, Keka…) + 173k-company list | **ATS-Scrapers** | Use as library + its `ats-companies/*.csv` |
| LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs, ZipRecruiter, Seek, Bayt, BDJobs | **JobSpy** | Library |
| Adzuna, Jooble, Wellfound, Dice, Hiring Cafe, The Muse, TrueUp, Handshake, CareerBuilder… | **JobDrop** | Library (extras only) |
| Remotive, Jobicy, Himalayas, Arbeitnow, Working Nomads, RemoteOK, WeWorkRemotely | **StillOpen** `scan.py` (Python, zero-token) | Copy connectors |
| Freehire (one search = ~90 job systems) | **JobRadar** `sources/boards/freehire.py` | Copy, replace its India-only filters with user prefs |
| Hacker News "Who is hiring", any careers page | **ApplyPilot** `hackernews.py`, `smartextract.py` | Copy |
| Telegram job channels (live) | **JobRadar** `sources/telegram.py` | Copy |
| India mass-hiring pages (TCS, Infosys, Wipro, Cognizant) | **JobRadar** `sources/pages/watcher.py` | Copy |

Common plug-in shape (JobRadar's design): every source is one module with `fetch() -> list[RawJob]`.

**Implemented (MVP):** `sources/ats.py` (ATS-Scrapers, one source per company board), `sources/boards.py` (JobSpy
public `scrape_jobs`; Naukri added for India, Seek for Australia), `sources/ingest.py` (store, quiet first-run
baseline, alert-priority `match` tasks). ATS-Scrapers is installed **without** its `[scrapers]` extra: that extra
downloads a stealth Chromium binary; without it ~93 of 99 hiring systems work and Meta/Tesla boards are unavailable.

**+1 (nobody does this):**
- **Smart polling:** each source gets its own speed. Telegram live, company job systems every 5–30 min, LinkedIn/Naukri slower to avoid blocks. Sources that often post new jobs get checked more.
- **Focus list:** from 173k companies, check often only the ones matching the user's roles + countries; rest covered by Freehire + board searches.
- **Source health:** a broken source is auto-detected and paused, and the user is told (idea from Career-Ops `dead-boards`).
- **Paste any careers link** → Sarthi auto-detects its job system (ATS-Scrapers `get_scraper_for_url`) and starts watching. Safe-URL check from StillOpen `netguard.py`.

### 2.2 Scheduler + task queue [MVP]
- **Copy JobRadar** `queue/tasks.py` + `queue/worker.py`: SQLite task table, retries with backoff (30s → 2m → 10m → 1h), never runs a step twice.
- **Copy JobRadar** board watcher rule: first check of a new source = save only, no alerts (no 500-message flood).
- **+1:** priority lanes — "new job alert" tasks always run before slow tasks (tailoring, applying), so alerts are never stuck behind work.

### 2.3 One job format [MVP]
- **Base model: ATS-Scrapers `Job`** (pydantic): has `country_iso` per location, salary with ISO currency, skills, education.
- **Add StillOpen `geo.py`** (copy as-is, no internet needed): turns "Bengaluru", "Remote (EU)", "London or remote UK" into country codes.
- **Add fields:** `remote_type` (onsite/hybrid/remote), `remote_countries` (empty = anywhere), `visa_sponsorship`, `first_seen_at`, `sources[]`.
- **Copy OpenApply "lean index":** store a 700-char snippet per job, fetch the full text only for jobs that pass matching → DB stays small (150k jobs ≈ 300 MB).

### 2.4 Dedupe [MVP]
- **Copy JobRadar:** clean-URL hash + company/role/location fingerprint; extra sources attach to the same job.
- **Copy Openings `duplicates.py`** (no dependencies): requisition id + title/company similarity signals.
- **Copy OpenApply `prior.ts` logic (port):** "you already applied to this company for a similar role" guard.

### 2.5 Fill missing details [MVP]
- **Copy JobRadar layered extraction:** page's built-in job data (JSON-LD) → labelled lines → AI only if role/company still missing. Saves most AI calls.
- **ApplyPilot `enrichment/detail.py`** for full description pages.

### 2.6 Quality gate [MVP]
| Check | Best code | Copy |
|---|---|---|
| Still open? | **JobRadar** `liveness.py` (asks Greenhouse/Lever/SmartRecruiters/Workday directly) | Copy |
| Scam? | **JobRadar** `scam.py` (no deps) | Copy as-is |
| Repost / ghost job? | **Career-Ops** `detect-reposts.mjs` | Port |
| Visa sponsor (UK) | **Career-Ops** `sponsor-check.mjs` | Port [Final] |

### 2.7 Match funnel [MVP] — cheapest first
| Layer | Cost | Best code |
|---|---|---|
| L0 Fast filter: title, keywords, location, salary, excluded companies | Free | **OpenApply** `match.ts` (port) |
| L1 Hard rules: country/visa eligible, years of experience, seniority | Free | **JobRadar** `match.py` hard rules + our `geo.py` |
| L2 AI score: set questions (eligible? role fit? skills? seniority? location?) → score 0–100 computed in code | 1 small AI call | **JobRadar** design (but local/any LLM instead of its paid Jev service) |
| L3 "Why it fits": each requirement → where your resume proves it | Free | **StillOpen** `matchcard.py` (port logic) |

ApplyPilot (AI 1–10 score for every job) is the most expensive approach — we only use AI at L2, for jobs that already passed L0+L1.

**Implemented (MVP):** `matching/rules.py` (L1, runs first: country / remote / visa from ISO codes, job type,
years required, exclusions, JobRadar scam check), `matching/prefilter.py` (L0, OpenApply port; seniority inferred
from years when unset), `matching/funnel.py` (AI only for jobs that pass both; `AiScorer` slot for L2),
`matching/decide.py`, `matching/handler.py` (`match` task → `job_match` row → `alert` task). Checked on 1,705 real
jobs for an Indian backend profile: 1,651 hidden (mostly outside the user's countries), 38 alerts, 16 digest.
The L3 evidence card (StillOpen matchcard) is not ported yet: L0 + rule notes supply the "why" lines for now.

**+1:** learns from your taps. Every Apply/Skip adjusts your alert threshold (JobRadar has the idea as a suggestion; we make it automatic).

### 2.8 Decide [MVP]
**Copy JobRadar `decide.py`** rule table, extended:
not eligible / scam → hide · strong + sure → **instant Telegram alert** · medium → **daily digest** · above auto-apply score + auto mode on → **apply directly** [Final].

### 2.9 Telegram bot — built by us (no repo has one) [MVP]
- **Onboarding chat:** upload resume, pick roles, countries, remote, visa, salary. (Preference shape from **Openings** `preferences.py`: `looking_for`, `ruled_out`, plus structured fields.)
- **Job alert:** title, company, place, salary, match %, 3 "why" lines, buttons **Apply · Skip · Later · Details**.
- **Before submit:** screenshot of the filled form + **Confirm** button.
- **Questions:** "This form asks your notice period — what should I answer?" → answer saved forever.
- **Commands:** `/status`, `/pause`, `/today`, `/prefs`, `/limit`.
- One `Channel` interface → **WhatsApp** plugs into the same code [Final].

**Implemented (MVP, 2026-11-20):** Bot API over plain `httpx` (`chat/telegram/api.py`, no bot library); one-time
pairing code from `sarthi pair` (hash stored, 15-minute expiry, cancelled after 5 wrong guesses; strangers get no
reply); job cards with Apply / Skip / Later / Details (job-site text HTML-escaped; first answer wins; Apply queues
an `apply` task); `/today`, `/status`, `/pause`, `/resume`, `/help`; daily digest at `SARTHI_DIGEST_HOUR` local
time; `app.py` is the composition root.
**Setup (MVP):** `/setup` conversation (`chat/setup.py`): resume upload (PDF/.docx/text, read locally by
`profile/resume.py`: size cap, type from bytes, no XML parser), AI suggestions from the scrubbed resume
(`ai/resume.py`), questions parsed by `profile/answers.py` (countries, remote, years, salary such as "12 LPA"),
summary with Save; saving re-matches jobs from the last 7 days. `/profile`, `/cancel`.
Answers are natural language: plain lists and "ok" use fast rules; anything else ("remove fintech, add ML
engineer instead") goes to the AI (`ai/answers.py`), whose list is then checked by rules (`reconcile`: items the
user never mentioned come back, explicit removals always apply), because a 3B local model drops and misses
items. Every answer is echoed back ("✓ Roles: …") with a Change button.
**`sarthi run` (MVP):** bot + worker + `sources/schedule.py`: a 1-minute tick queues due sources (boards hourly
with the profile's roles/countries, company boards every 30 min); each check looks back to its last success
(catch-up after the computer was off, §2.18); failing sources back off up to 8x. Every new job is matched
(JobRadar's quiet first run is not used: matching decides what alerts, and the look-back window drops old jobs).

### 2.10 Documents [MVP]
| Need | Best code | Copy |
|---|---|---|
| Tailored resume + PDF | **ApplyPilot** `scoring/tailor.py`, `pdf.py`, `json_resume.py` | Copy |
| Cover letter | **ApplyPilot** `scoring/cover_letter.py` | Copy |
| No invented facts | **OpenApply** `truth.ts` (port) + **Resume-Matcher** `resume_preservation.py` | Combine |
| ATS can read the PDF | **Resume-Matcher** `ats_parse/checks.py` | Copy [Final] |

**+1:** documents are made only for jobs you tap Apply on (or auto-apply jobs), not every match → far fewer AI calls.

### 2.11 Form answers [MVP]
- **Copy OpenApply `questions.ts` (port):** read the form's real questions from Greenhouse/Ashby/Lever **before** opening a browser.
- **Copy ApplyPilot `qa_knowledge` table:** every answer saved and reused, with outcome.
- **+1:** unknown questions are asked on Telegram **together with the job alert**, so when you tap Apply nothing blocks the application.

### 2.12 Apply engine [MVP] — four-step ladder
| Step | What | Best code |
|---|---|---|
| 1. Rule-based fill | Known job systems filled with plain code, no AI | **OpenApply** `apply/adapters/*` + `fieldmap.ts` (port to Python) |
| 2. Replay | Repeat the click path that worked last time on the same job system | **ApplyPilot** `successful_paths.py` |
| 3. AI browser | AI fills any unknown form, any website | **AIHawk** `apply.py` (works with Ollama or any OpenAI-style API) |
| 4. Hand to user | CAPTCHA / login wall → send link + resume + answers on Telegram | **OpenApply** `captcha.ts` detection |

Plus, copied:
- **Approval tied to one fill** (OpenApply): submit only works with the code from that exact preview.
- **Passwords encrypted** (AIHawk `secret.py`) — never in AI prompts (ApplyPilot's flaw removed).
- **Site logins remembered** per site in a saved browser profile (ApplyPilot accounts + AIHawk session).
- **Proof:** screenshot of the confirmation page (AIHawk).
- **Daily limit per site** (user-set) to protect the user's LinkedIn/Naukri accounts.

**+1:** steps 1–2 cost zero AI; AI is only used when the cheap ways fail, and every AI success is saved as a replay path → each job system gets cheaper over time.

### 2.13 Tracking replies [MVP]
- **Copy ApplyPilot `tracking/*`:** Gmail reading, AI sorting (confirmation / rejection / interview / offer), people + dates, ghosted after N days.
- **Copy JobRadar `company_replies.py` + `followups.py`:** follow-up reminder with a draft [Final].
- **+1:** IMAP option with app password (works with any email, not only Gmail OAuth); every reply → Telegram message.

### 2.14 AI layer [MVP]
- **Plain `httpx` against OpenAI-compatible endpoints** (decided 2026-11-20, replacing LiteLLM): Ollama, Gemini,
  OpenAI, Anthropic, Groq, OpenRouter and DeepSeek all speak that API, so one small client covers them with far
  fewer dependencies in a tool that holds resumes and keys. Users list `<provider>/<model>` in the order to try.
- **Copy ApplyPilot** fallback chain (provider down → next one).
- **Copy JobRadar:** cache AI answers per job, daily AI budget, job text passed only as data (prompt-injection safe), only a compact profile sent (no name/email/phone).
- Two model slots: **fast** (matching) and **quality** (resume, cover letter, forms).
- Daily budget counts **tokens** of paid providers (local Ollama is exempt); full job text is stored compressed
  (`job_text`, 12k chars) so the AI judges the whole posting, not the snippet.
- **Implemented (MVP):** `ai/providers.py`, `ai/client.py`, `ai/prompts.py` (fenced untrusted data),
  `ai/privacy.py` (emails, phones, links removed from the resume), `ai/scoring.py` (L2 of the match funnel).

### 2.15 Storage [MVP]
One SQLite file. Tables: `sources`, `jobs`, `job_sources`, `matches`, `applications`, `answers` (Q&A bank), `accounts` (encrypted), `apply_paths`, `emails`, `tasks`, `llm_cache`, `events`.
**Copy Career-Ops rule:** user data (resume, prefs, DB) separated from app files → updates never touch user data.

### 2.16 Run & install [MVP]
`docker compose up` → Sarthi + Playwright browser (+ optional Ollama). Setup finishes inside Telegram.

### 2.17 Dashboard [Final]
Small local web page: pipeline, applied, replies, stats. (Patterns from OpenApply `ui` / JobScan.)

### 2.18 Catch-up mode — for users who can't run Sarthi all day [MVP]
User picks a time window: **last 24 hours · 2 days · 5 days · 7 days**. Sarthi searches every source for jobs posted in that window, runs the full pipeline (dedupe → quality gate → match), and shows the results as one batch.

How each source searches by date (verified in code):
| Source | Date filter |
|---|---|
| LinkedIn, Indeed, Naukri, Glassdoor… | **JobSpy** `hours_old` parameter |
| Freehire | API `open_within_days` |
| Company job systems | **ATS-Scrapers** returns all open jobs with `posted_at` → filter by date |
| Free feeds, HN, Telegram channels | posted date in the data / message history catch-up (JobRadar Telethon catch-up) |
| Jobs with no posted date | `first_seen_at` (when Sarthi first saw it) |

Same idea exists in **AIHawk** `library.py` (search "looks back N days", default 15) — we copy the concept.

After the batch is ready, user chooses on Telegram:
1. **Apply for me (batch)** — Sarthi applies one by one with safe pacing:
   - per-site daily limit (e.g. LinkedIn 15/day, Naukri 20/day — user can change);
   - random gaps between applications (minutes, not seconds);
   - leftover jobs carry over to the next run, best matches first;
   - one summary message at the end: applied / needs you / failed.
2. **Give me the list (I'll apply myself)** — Sarthi sends:
   - an Excel/CSV file: company, role, location, salary, match %, why it fits, apply link;
   - tailored resume PDF + cover letter + ready form answers for each job (zip), for the top matches;
   - user taps "Applied" on each so tracking still works.
3. **Mix** — auto-apply on easy sites, list for the rest.

**Scheduled catch-up:** "every day at 9 PM, check last 24 hours" — Sarthi starts, does the batch, sends results. Works with a computer that is on only part of the day. If the computer was off for 3 days, the next start automatically catches up on those 3 days.

**+1:** no repo combines a time-window search over all sources with batch apply + safe pacing + a do-it-yourself pack.

---

## 3. Where Sarthi beats every repo
| Thing | Best repo today | Sarthi |
|---|---|---|
| Sources | ATS-Scrapers ~100 / JobDrop 38 | All combined: 150+ plus Telegram + India drives |
| Speed | JobRadar 20–30 min fixed | Per-source smart polling, live Telegram |
| Matching | One method each | 4-layer funnel, AI only where needed, learns from taps |
| Applying | Each repo has 1 method | 4-step ladder, gets cheaper over time |
| Talking to user | Email / web page / terminal | Telegram bot (+ WhatsApp), apply from the phone |
| Form questions | Asked mid-application or guessed | Asked up front with the alert, saved forever |
| Passwords | ApplyPilot plaintext in prompts | Encrypted, never sent to AI |
| Cost to user | Paid AI plans in several | Free with Ollama, or own key |
| Not running all day | Mostly unsupported / jobs missed | Catch-up mode: last 24h–7 days, batch apply with safe pacing, or a ready-to-apply pack |

## 4. MVP vs Final
**MVP:** 2.1–2.16 + 2.18 (all sources, queue, format, dedupe, quality gate except UK visa, 4-layer match, Telegram bot, documents, answers, 4-step apply, Gmail tracking, LiteLLM, Docker, catch-up mode with batch apply + export).
**Final:** WhatsApp, full auto-apply mode, UK visa sponsor check, ATS-readable PDF checks, follow-up drafts, IMAP, dashboard, more rule-based apply adapters (LinkedIn Easy Apply, Workday, Naukri).

## 5. Folder structure

Pattern: **one folder per pipeline step + plug-in folders** (JobRadar / ApplyPilot style; repos audited use
step folders, per-site plug-ins, or web-app layers — no shared standard; MVC fits websites, Sarthi is a worker + bot).

```
sarthi/
├── src/sarthi/
│   ├── sources/        # 2.1  one module per source group (base.py = Source protocol) + liveness check
│   ├── queue/          # 2.2, 2.18  task queue, workers, scheduler, catch-up
│   ├── jobs/           # 2.3–2.6  pure job model, geo, dedupe, normalise, scam/repost checks (no I/O)
│   ├── matching/       # 2.7–2.8  match funnel + decide
│   ├── documents/      # 2.10 resume, cover letter, truth check, PDF
│   ├── answers/        # 2.11 form questions + answer bank
│   ├── apply/          # 2.12 apply ladder
│   │   └── adapters/       # rule-based fillers per job system
│   ├── tracking/       # 2.13 replies, ghosting, follow-ups
│   ├── chat/           # 2.9  Channel protocol
│   │   ├── telegram/
│   │   └── whatsapp/       # Final
│   ├── ai/             # 2.14 LiteLLM, fallback, cache, budget, prompts
│   ├── db/             # 2.15 schema + data access (only place with SQL)
│   ├── profile/        # resume + preferences
│   ├── config.py  log.py  cli.py
├── tests/              # mirrors src/sarthi/
├── data/               # user data, git-ignored, never touched by updates
├── docker/  docs/  .github/workflows/
├── LICENSE  THIRD_PARTY_NOTICES.md  SECURITY.md  pyproject.toml
```

## 6. Rules the code follows

Full list in [STANDARDS.md](STANDARDS.md). The architecture-level ones, enforced by `import-linter` in CI:
- Pipeline steps (`sources`, `matching`, `documents`, `answers`, `apply`, `tracking`, `chat`) never import each other; they talk only through `db` + `queue`.
- Layers import downwards only: `cli` → steps → `queue / ai` → `profile` → `db` → `jobs` (pure job model, no I/O) → `clock / config / log`.
- Only `db` writes SQL; only `ai` calls LLMs.

## 7. Not yet proven
Everything above is from reading code. Before building, each copied piece must pass its own tests and a live run.
