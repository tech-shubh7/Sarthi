<p align="center">
  <img src="docs/brand/sarthi-wordmark-tagline-light.svg" alt="Sarthi, your AI job assistant" width="440">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-AGPL--3.0-233A8B" alt="Licence: AGPL-3.0"></a>
  <img src="https://img.shields.io/badge/python-3.12-233A8B" alt="Python 3.12">
  <img src="https://img.shields.io/badge/status-early%20development-F4A024" alt="Status: early development">
</p>

# Sarthi (सारथि)

**A job-search assistant that runs on your own computer.** Sarthi reads job boards, company career pages and
free job feeds all day, keeps the jobs that genuinely fit you, sends them to you on Telegram, tailors your
resume and cover letter for each one, fills in the application when you say yes, and tells you when a
company replies.

It is free and open source (AGPL-3.0). Your resume, your answers and your logins stay on your machine.

> **Status:** early, under active development, heading for the first public release (v0.1). Expect rough
> edges, and please [open an issue](https://github.com/tech-shubh7/Sarthi/issues) when you hit one.

---

## Why Sarthi

Looking for a job is mostly repetition: searching the same sites every morning, reading postings that turn
out to want five more years than you have, rewriting the same cover letter, and typing your name and email
into one more form. Sarthi does that part, and leaves the decisions to you.

- **It tells you the truth about fit.** Every match lists what you have *and* what you are missing. A job is
  scored on whether your resume shows its must-have skills, not on how many keywords it shares with you.
- **Nothing is sent without you.** Sarthi fills the form and shows it to you first. Auto-apply exists, but
  it is off until you switch it on, and even then you get a window to cancel.
- **Your data stays yours.** No account, no cloud database. Contact details are removed before any text
  goes to an AI model.

## What it does

| | |
|---|---|
| 🔎 **Finds jobs** | LinkedIn and Indeed (plus Naukri, Seek and others by country), free remote-job feeds, Hacker News "Who is hiring", company career pages on Greenhouse, Lever, Ashby, Workday and ~90 more hiring systems, and optional Adzuna/Jooble keys. |
| 🎯 **Matches honestly** | Hard rules first (country, visa, years, seniority, scams), then an AI checks each must-have against your resume. Wrong-role jobs are hidden, not shown at 70%. |
| 📲 **Alerts you on Telegram** | Strong matches come straight away (at most a few an hour), the rest in a daily digest. Tap Apply, Skip or Later. |
| 📄 **Tailors your documents** | Your own resume, in your own design, with each line reworded toward the job (same length, same links and contact details), and a short cover letter, both checked so they never claim anything your resume does not say. |
| 📝 **Fills applications** | Greenhouse, Lever and Ashby forms directly, other career sites through a careful AI browser. Questions only you can answer are asked once in the chat and remembered. |
| 🤖 **Auto-apply (optional)** | Above a match % you choose, with a 5-minute window to cancel, a daily cap, and never on job boards. |
| 📬 **Tracks replies** | Reads (never sends or deletes) your mailbox and tells you about interviews, tests and rejections. |
| 🖥️ **Dashboard** | A local web page to see everything, change your profile and settings, apply, and approve filled forms. |

## Quick start (Docker)

You need [Docker](https://docs.docker.com/get-docker/) and a free AI key (a
[Gemini key](https://aistudio.google.com/apikey) works well).

```bash
git clone https://github.com/tech-shubh7/Sarthi.git && cd Sarthi
cp .env.example .env      # open it and paste one free AI key, e.g. SARTHI_GEMINI_API_KEY=...
docker compose up -d      # the first start builds the image: about 10 minutes and 3.5 GB
```

Then open **http://127.0.0.1:8765**. Sarthi starts searching right away. **Telegram is optional:** the
dashboard does everything on its own (matches, applying, form questions, Submit, auto-apply, site logins,
replies and follow-up drafts). Telegram only adds alerts on your phone.

1. **Set up your profile.** Open **Settings** on the dashboard, add your resume (PDF, .docx, LaTeX .tex or
   .txt), the job titles you want and the country you live in, and save. (Or, once Telegram is connected,
   send `/setup` to the bot and upload your resume: it suggests job titles and skills for you to confirm.)
   Sarthi keeps the file (in `data/resume/`, readable only by you) and tailors resumes **in its design**:
   a PDF has its changed lines rewritten in place when the new words fit; a .tex or .docx source is
   edited in place and compiled when a LaTeX engine (Tectonic) or LibreOffice is installed, otherwise the
   edited source is saved next to a PDF in Sarthi's own layout. Your links and contact details are always
   kept; a resume that would drop one is not made.
2. **Connect Telegram (optional)** to get alerts on your phone: tap *Connect Telegram* on the dashboard's
   Overview, then **Create** and **Start** in Telegram. Sarthi's connector makes you a bot of your own, no
   @BotFather and no token to copy ([how it works](deploy/telegram-connect/README.md)). Prefer your own
   bot? Set `SARTHI_TELEGRAM_BOT_TOKEN` to a token from [@BotFather](https://t.me/BotFather). No Telegram
   at all? Set `SARTHI_TELEGRAM_CONNECT_URL=` (empty). After changing `.env`, run `docker compose up -d`
   again (a plain `restart` does not re-read it).
3. **Wait for matches.** The first ones usually arrive within the hour, on the dashboard's **Matches**
   page and in Telegram.

Everything Sarthi keeps (database, resume, documents, site logins) lives in `./data`. Stop it with
`docker compose down`; your data stays. On Linux, the container runs as user id 1000: if `id -u` says
otherwise, run `sudo chown -R 1000:1000 data` once.

## Using Sarthi

### In Telegram

| Command | What it does |
|---|---|
| `/setup` | Create or redo your profile from your resume |
| `/profile` | Show your profile. Change anything by just writing it, e.g. *add UAE to my countries* |
| `/today` | The best matches you have not seen yet |
| `/catchup` | Everything posted in the last 1 to 7 days, in one batch |
| `/status` | What Sarthi has found and sent so far |
| `/pause` · `/resume` | Stop or restart instant alerts (matches still wait for you) |
| `/login` | Log in once to LinkedIn, Naukri, Indeed and other boards, so Sarthi can apply there |
| `/autoapply 85` · `/autoapply off` | Turn auto-apply on from 85% (75 to 100), or off |

Each job card has **Apply**, **Skip**, **Later**, **Details** and **Tailored resume** buttons. After you
tap Apply you get the tailored resume and letter, any questions the form asks, and finally a screenshot of
the filled form with **Submit** and **Cancel**.

### On the dashboard (http://127.0.0.1:8765)

- **Overview:** the whole funnel at a glance, and what waits for you.
- **Activity:** questions an application form asks (answer them here), auto-applies you can still cancel,
  and everything Sarthi did: documents made, applications sent, employer replies and follow-up drafts.
- **Matches:** every match from the last week with its score; Apply, Later or Skip from here.
- **Applications:** filled forms waiting for your Submit (with a screenshot), and everything sent.
- **Sources:** which job sources are working and which are not.
- **Settings:** your profile (including a new resume), pausing alerts, the auto-apply line and job site logins.

The dashboard and Telegram are two views of the same thing: an answer given in one shows in the other.
It is reachable from your computer only, and every change has to come from its own pages.

### Logging in to job boards

Boards such as LinkedIn and Naukri only let you apply when you are signed in. On the dashboard, use
**Settings → Job site logins** (or send `/login` in Telegram), pick the site, and open the link Sarthi gives you: it shows that site's real login page in your browser
(http://127.0.0.1:6080 under Docker). You type your password into the site itself. Sarthi keeps only the
session cookie, in `./data`, and never sees or stores your password.

### Auto-apply

Off by default. When you set a line (say 85%), each new match at or above it is announced on the dashboard's
Activity page (with Cancel and Apply now) and in Telegram, if connected:
*"I'll apply to Backend Engineer at Acme in 5 minutes unless you cancel."* If you do nothing, Sarthi prepares
the documents and fills the form. It submits by itself only if every answer came from your profile or your
saved answers, the site is a company career page (never LinkedIn, Naukri or another job board), and fewer
than 10 have gone out today. Anything else comes back to you as a normal preview. Tapping Apply yourself
always gives you the normal preview.

## AI models

Sarthi works with free cloud tiers, paid APIs or a model on your own computer. List models in `.env`, best
first; when one is busy or out of quota, the next one answers. The lists in `.env.example` already cover
the free Gemini, Groq and OpenRouter tiers, so a key for any one of them is enough to start; models whose
key is empty are skipped.

```ini
SARTHI_LLM_FAST_MODELS=["gemini/gemini-3.5-flash-lite","openrouter/nvidia/nemotron-3.5-super-vl:free"]
SARTHI_LLM_QUALITY_MODELS=["gemini/gemini-3.5-flash","gemini/gemini-3.5-flash-lite"]
SARTHI_GEMINI_API_KEY=your-key
```

Supported providers: Gemini, Groq, OpenRouter, OpenAI, Anthropic, DeepSeek, and Ollama for local models.
*Fast* models score jobs (many small calls); *quality* models write your documents. Matching never runs
on a local model, so your laptop stays usable. Free tiers have daily limits: when they run out, matching
waits for the next window instead of guessing.

Before any text reaches a model, your email, phone number and links are removed. Free tiers may still keep
what they receive, so use a paid key or a local model if that matters to you.

## Configuration

All settings live in `.env` (see [.env.example](.env.example) for every option with notes). The ones you
are most likely to touch:

| Setting | Default | What it does |
|---|---|---|
| `SARTHI_JOB_BOARDS` | `["linkedin","indeed"]` | Job boards searched for your roles |
| `SARTHI_FEEDS` | all free feeds | Remote-job feeds; `[]` turns them off |
| `SARTHI_WATCH_COMPANIES` | `[]` | Company boards to watch, e.g. `["greenhouse/razorpay"]` |
| `SARTHI_DIGEST_HOUR` | `19` | When the daily digest arrives (local time) |
| `SARTHI_APPLY_DAILY_LIMIT` | `30` | Applications per site per day |
| `SARTHI_AUTO_APPLY_MIN_SCORE` | off | Auto-apply line (75 to 100); also settable in the chat |
| `SARTHI_MAIL_IMAP_HOST` … | off | Mailbox to watch for replies (read-only; Gmail needs an app password). Easier: Settings → Mail on the dashboard, with Test connection |

## Privacy and safety

- **Local first.** Sarthi runs on your computer; its dashboard listens on 127.0.0.1 only.
- **No passwords.** Site logins happen on the site itself; mail access uses an app password you can revoke.
- **No made-up claims.** Tailored resumes and letters are checked line by line against your own resume.
- **No CAPTCHA solving, no bot-detection tricks.** When a site asks for a human, Sarthi hands it to you.
- **Limits on purpose.** Daily caps per site, a cap on auto-applies, and no repeat applications to the
  same role at the same company.

Found a security problem? Please follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Running without Docker

Python 3.12 and [uv](https://docs.astral.sh/uv/) are needed.

```bash
git clone https://github.com/tech-shubh7/Sarthi.git && cd Sarthi
uv sync
uv run playwright install chromium   # on Linux: add --with-deps (installs Chromium's libraries via sudo)
cp .env.example .env                 # then paste one free AI key, as in the Quick start
uv run sarthi run                    # dashboard at http://127.0.0.1:8765
```

Then follow steps 1 to 3 of the Quick start (after changing `.env`, stop Sarthi with Ctrl+C and run it
again). `uv run sarthi --help` lists every command.

## FAQ

**Will it spam companies with applications?** No. Nothing is submitted without your tap unless you turn on
auto-apply, which has a veto window, a daily cap and stays off job boards.

**Why did a job I like score low?** Open it in Telegram: the ✗ lines say what the posting requires that
your resume does not show. If your resume does show it, add the skill in Settings or `/profile`.

**The AI says it is busy.** Free tiers have per-minute and per-day limits. Add a second provider to your
model lists, or wait; jobs are matched once a model answers again.

**Can I use it without Telegram?** Yes. Telegram is optional: the dashboard does everything. Apply from
Matches, answer form questions on Activity, check and submit the filled form on Applications, and log in to
job sites from Settings. You can connect Telegram later and use both; an answer given in one shows in the other.

## Contributing

Bug reports, ideas and pull requests are welcome.

```bash
uv sync
uv run pre-commit install   # lint, types, architecture rules and secret scanning on every commit
uv run pytest               # the suite; coverage must stay above 80%
```

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit and
[docs/STANDARDS.md](docs/STANDARDS.md) for the coding and security rules. Please keep test data fictional.
The logo, colours and how to use them are in [docs/BRAND.md](docs/BRAND.md).

## Acknowledgements

Sarthi stands on the work of other open-source projects, among them
[ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), [JobRadar](https://github.com/ThePlator/JobRadar),
[JobSpy](https://github.com/speedyapply/JobSpy), [ATS-Scrapers](https://github.com/kalil0321/ats-scrapers),
[OpenApply](https://github.com/nturl/openapply), [StillOpen](https://github.com/Druvo/stillopen),
[Resume-Matcher](https://github.com/srbhr/Resume-Matcher) and
[AIHawk](https://github.com/feder-cr/Jobs_Applier_AI_Agent). What came from where, with licences, is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Licence

[AGPL-3.0-only](LICENSE). You may use, change and share Sarthi freely; if you run a modified version as a
service for others, you must share your changes under the same licence.
