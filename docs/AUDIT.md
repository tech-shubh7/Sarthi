# Sarthi — Reference Repo Audit (2026-11-20)

All 14 repos shallow-cloned into `references/` (read-only, never committed with the product).
Licences verified from each repo's LICENSE file. Sarthi will be **AGPL-3.0** (inherits from ApplyPilot).

## 1. Verdict per repo

| Repo | Lang / size | Licence | Verdict |
|---|---|---|---|
| **ApplyPilot-ibarrajo** | Python, 28k LOC, 30 tests | AGPL-3.0 | **BASE** → becomes `product/` |
| ApplyPilot-upstream | Python, 9.6k LOC, 0 tests | AGPL-3.0 | Drop. ibarrajo is a strict superset (same files, more features) |
| **ATS-Scrapers** | Python, ~100 ATS connectors, 173k-company catalog | MIT | **USE AS DEPENDENCY** (`ats-scrapers`) + its `ats-companies/*.csv` catalog |
| **JobSpy** | Python | MIT | Keep as dependency (already in base). LinkedIn/Indeed/Glassdoor/Naukri/Google/Seek/Bayt |
| JobDrop | Python, 38 sources | MIT | Optional dependency later — only for extras JobSpy lacks (Adzuna, Jooble, Wellfound, Dice, Hiring Cafe, The Muse) |
| **JobRadar** | Python, 16k LOC, 45 tests | MIT | **COPY selected modules** (monitoring core) |
| **AIHawk** | Python, 5k LOC | MIT | **COPY apply engine** — replaces ApplyPilot's Claude-CLI apply |
| Freehire | Go/TS, 1.2M LOC | MIT | **No code copy.** Call its free keyless API `freehire.me/api/v1/jobs/search` (JobRadar already has a reader) |
| **StillOpen** | Python/Django | Apache-2.0 | **COPY 2 standalone files** (geo, netguard) |
| Openings | Python | MIT | **COPY 1 standalone file** (duplicates) + preference-model ideas |
| Resume-Matcher | Python/TS | Apache-2.0 | Phase 2: resume anti-hallucination + ATS parse checks |
| OpenApply | TypeScript | MIT | **PORT idea**: deterministic ATS form-filling before LLM |
| Career-Ops | JS, 637k LOC | MIT | Ideas only (UK visa-sponsor check, repost detection, robots check) |
| JobScan | TS | MIT | Skip — nothing we don't already get elsewhere |

## 2. Base: ApplyPilot-ibarrajo — keep / fix / remove

**Keep (works, matches our pipeline):**
`pipeline.py`, `database.py` (tables: jobs, accounts, tracking_emails, tracking_people, qa_knowledge, job_state_transitions), `discovery/{jobspy,greenhouse,lever,ashby,workday,hackernews,smartextract,url_normalize,ats_common}.py`, `enrichment/detail.py`, `scoring/{scorer,tailor,cover_letter,pdf,validator,json_resume}.py`, `tracking/*` (Gmail reply tracking), `apply/{hitl,human_review,result_handlers,successful_paths}.py`, `llm.py` (already supports `LLM_URL` for Ollama/llama.cpp + Gemini + OpenAI fallback chain).

**Must fix (verified in code):**
1. `apply/launcher.py:2065` spawns `claude -p --permission-mode bypassPermissions` per worker → needs a paid Claude plan and gives the AI full system access. **Replace with AIHawk's apply engine.**
2. `database.py:213` stores site passwords in plain text; `apply/prompt.py:282-296` pastes them into the LLM prompt. **Replace with AIHawk `secret.py` (Fernet, key file 0600) and never put secrets into the prompt.**

**Remove:** `apply/extension/*` (Chrome extension, 2.4k lines JS), `discovery/{amazon,costco,builtin}.py` (single-company scrapers; ATS-Scrapers covers these), Claude-specific MCP config in launcher.

## 3. Exactly what to take from each repo

### AIHawk → apply engine (adapt imports: config, database.Job, llm, store)
- `src/aihawk/apply.py` (912) — `fill_form`, `submit_and_verify`, `apply_to_job`: model fills form via Playwright MCP, **user approves before submit**, verifies result page, keeps proof screenshots. Works with any OpenAI-compatible endpoint → Ollama works.
- `src/aihawk/secret.py` (48) — zero deps, copy as-is.
- Look at: `liveness.py`, `session.py` (browser profile persistence).

### ATS-Scrapers → discovery (dependency, no copy)
- `get_scraper("greenhouse","slug").fetch()` / `get_scraper_for_url(url)` / `find_company(name)`.
- Pydantic `Job` model with `JobLocation.country_iso`, `Salary.currency` (ISO 4217) → **fits our global schema directly.**
- India-relevant ATS present: Darwinbox (203 companies), Keka (393). Global: Workday, iCIMS, Taleo, SuccessFactors, Oracle, Personio, Teamtailor, SEEK, USAJobs, MyCareersFuture, EURES, etc.
- Eventually replaces base's own greenhouse/lever/ashby/workday modules (compare quality first).

### JobRadar → always-on monitoring core
| File | Lines | Coupling | Use |
|---|---|---|---|
| `queue/tasks.py` + `queue/worker.py` | 102+96 | jobradar.db.repo | SQLite task queue with retry/backoff — adapt to our DB |
| `sources/boards/watcher.py` | 256 | medium | Per-board due-time scheduling, first read = baseline (no alert spam) |
| `sources/boards/freehire.py` | 340 | high | Freehire API reader — **replace its hardcoded India/tech/not-senior filters with per-user prefs** |
| `pipeline/scam.py` | 93 | none | Copy as-is |
| `pipeline/liveness.py` | 172 | low | Is-posting-still-open check (Greenhouse/Lever/SmartRecruiters/Workday) |
| `pipeline/decide.py` | 89 | config | Ordered rule table: hidden / notify / etc. |
| `pipeline/extract.py` + `structured.py` | 328+204 | medium | Cheap-first extraction (schema.org JobPosting before LLM) |
| `sources/pages/watcher.py` | 231 | medium | India mass-hiring drive pages (TCS NextStep, Infosys, Wipro, Cognizant) |
| `sources/telegram.py` | 188 | medium | Phase 2: read public Telegram job channels (Telethon, read-only) |
| `sinks/followups.py`, `sinks/company_replies.py` | 309+348 | medium | Compare with base `tracking/*`; keep the better one |

### StillOpen → global geo + safety
- `tracker/services/geo.py` (342) — **zero deps, copy as-is.** `resolve_place(text)` → ISO country codes or `{"*"}` for anywhere; offline. Core of the country-preference engine.
- `tracker/services/netguard.py` (188) — SSRF guard for user-supplied career-page URLs. Copy.
- `tracker/services/fit.py` — idea only (Django-coupled): fit warnings, never silently hides.

### Openings
- `backend/openings/duplicates.py` (394) — zero deps, copy. Cross-source duplicate detection (company/title/places/requisition ids).
- `preferences.py` — idea: free-text preference sections (`looking_for`, `ruled_out`, …) → good fit for chat-based onboarding.

### Resume-Matcher (phase 2)
- `services/resume_preservation.py` — deterministic guard stopping the AI from inventing resume facts.
- `services/ats_parse/checks.py` — deterministic "can an ATS parse this PDF" checks.
- Compare against base `scoring/validator.py` before taking.

### OpenApply (TS → port ideas to Python)
- `src/apply/fieldmap.ts` (347) + `adapters/{greenhouse,lever,ashby}.ts`: fill known ATS forms **deterministically** from profile; use the LLM only for leftover fields → far fewer tokens per application, more reliable. Combine with AIHawk as fallback.
- `src/truth.ts`: check cover letter/answers don't claim figures absent from the resume.

### Career-Ops (ideas only)
- `sponsor-check.mjs` — UK Skilled Worker sponsor-register cross-check (visa filter for UK).
- `detect-reposts.mjs`, `robots-check.mjs`.

## 4. Gaps — nobody has these, we build them
1. **Telegram bot for the user** (Apply / Review / Skip buttons, onboarding chat). JobRadar's Telegram is a channel *reader*, not a bot. Use `python-telegram-bot`.
2. **Per-user preference engine**: countries, remote type, visa per country, roles, salary — built on `geo.py` + ATS-Scrapers' `country_iso`.
3. **Unified job schema + source manager** joining base DB, ATS-Scrapers, JobSpy, Freehire.
4. Deterministic-first apply (OpenApply port) in front of AIHawk.
5. Docker packaging, `THIRD_PARTY_NOTICES.md`.

## 5. Proposed build order
1. Copy ApplyPilot-ibarrajo → `product/sarthi`, run its 30 tests, `git init`.
2. Remove Claude-CLI apply + extension + plaintext credentials; plug in AIHawk `apply.py` + `secret.py`.
3. Add ATS-Scrapers dependency + company catalog; copy `geo.py`, `duplicates.py`, `scam.py`, `netguard.py`.
4. Port JobRadar queue/worker + board watcher + liveness → continuous monitoring.
5. Telegram bot: notify → Apply/Skip → apply → status.
6. Freehire reader (per-user filters), Docker, notices, README.
7. Phase 2: deterministic ATS adapters, Resume-Matcher guards, Telegram channel reader, India drive pages, UK sponsor check, JobDrop extras.

## 6. Risks
- Job boards (LinkedIn, Indeed, Naukri) restrict scraping; prefer the public company job APIs (ATS-Scrapers, Freehire) and keep board scraping opt-in.
- Freehire is a third-party hosted API: rate-limit and fail gracefully.
- Not yet verified by running: none of the connectors have been run live; step 1 must run tests and a live fetch smoke test.
