<p align="center">
  <img src="docs/brand/sarthi-wordmark-tagline-light.svg" alt="Sarthi, your AI job assistant" width="440">
</p>

<h3 align="center">Finds the jobs that fit you, and applies to them for you.<br>
Free, open source, and it runs on your own computer.</h3>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-AGPL--3.0-233A8B" alt="Licence: AGPL-3.0"></a>
  <img src="https://img.shields.io/badge/python-3.12-233A8B" alt="Python 3.12">
  <img src="https://img.shields.io/badge/runs-on%20your%20computer-20B486" alt="Runs on your computer">
  <img src="https://img.shields.io/badge/status-early%20development-F4A024" alt="Status: early development">
</p>

<p align="center">
  <b>LinkedIn</b> · <b>Indeed</b> · <b>Naukri</b> · <b>~430 job sites</b> · <b>~192,000 company career pages</b>
</p>

https://github.com/user-attachments/assets/467061aa-a120-45f4-8b9d-bd82fc4006f6

Job hunting means hours of searching, reading postings that don't fit, and typing the same details into
form after form. **Sarthi does that part for you.** Give it your resume and the jobs you want. Every hour
it searches all the sources above, keeps only the jobs that really fit your resume and preferences, and
tells you. Press **Apply** and it writes a cover letter for that job, fills in the form and sends it when
you say so. Or turn on auto-apply and let it send by itself.

No account, no cloud server, no subscription. Your resume, your answers and your site logins stay on your
computer.

<p align="center">
  <a href="#install"><b>Install</b></a> &nbsp;·&nbsp;
  <a href="#first-time-setup-step-by-step"><b>Set up</b></a> &nbsp;·&nbsp;
  <a href="#how-matching-works"><b>How matching works</b></a> &nbsp;·&nbsp;
  <a href="#troubleshooting-and-faq"><b>FAQ</b></a>
</p>

## How it works

| Step | What happens |
|---|---|
| **1. Finds jobs** | Searches LinkedIn, Indeed, your country's job boards, ~430 job sites, free remote feeds and a daily snapshot of ~192,000 company career pages, for your job titles and countries |
| **2. Keeps only what fits** | Scores each job on the must-haves your resume really shows, with a clear ✓ have / ✗ missing list. Hides the wrong country or visa, far more years than you have, senior roles when you are junior, and scams |
| **3. Tells you** | Strong matches appear on the dashboard (and in Telegram) at once. The rest come in one daily digest |
| **4. Applies** | Writes a cover letter for the job (and, if you turn it on, a tailored resume), fills the form with your details and shows it to you before anything is sent. With auto-apply, it sends by itself |
| **5. Hands you the human steps** | CAPTCHAs, sites that block robots and expired logins go to clear lists, each finished in a few clicks |
| **6. Watches your inbox** | Optional. Tells you when a company replies: an interview, a test or a rejection |

> **Status:** early, under active development, heading for the first public release (v0.1). Expect rough
> edges, and please [open an issue](https://github.com/tech-shubh7/Sarthi/issues) when you hit one.

---

## Contents

- [What is automatic and what needs you](#what-is-automatic-and-what-needs-you)
- [What you need](#what-you-need)
- [Install](#install): [with Docker](#option-a-docker-windows-mac-linux) or [without Docker](#option-b-without-docker)
- [First-time setup, step by step](#first-time-setup-step-by-step)
- [Using it every day](#using-it-every-day)
- [Where the jobs come from](#where-the-jobs-come-from)
- [How matching works](#how-matching-works)
- [Your resume and cover letter](#your-resume-and-cover-letter)
- [Applying and auto-apply](#applying-and-auto-apply)
- [When Sarthi needs you](#when-sarthi-needs-you)
- [AI models and what they cost](#ai-models-and-what-they-cost)
- [Settings](#settings)
- [Privacy and safety](#privacy-and-safety)
- [Troubleshooting and FAQ](#troubleshooting-and-faq)
- [Contributing](#contributing) · [Acknowledgements](#acknowledgements) · [Licence](#licence)

## What is automatic and what needs you

| Sarthi does this by itself | You do this |
|---|---|
| Searches for jobs every hour, all day | Give it your resume, the job titles you want and your country (once) |
| Scores each job against your resume and hides the wrong ones | Press **Apply** on the jobs you want, or set an auto-apply line |
| Writes a cover letter for each job you apply to | Log in to LinkedIn, Naukri, Indeed and similar sites once, in a window Sarthi opens |
| Fills in application forms: Greenhouse, Lever, Ashby, LinkedIn Easy Apply, Naukri and most company career pages | Answer a form question it has never seen. It asks once and remembers the answer |
| Sends the filled form when you press Submit, or by itself if you allowed that | Solve CAPTCHAs (Sarthi never solves them) |
| Remembers what it applied to, so it never applies to the same job twice | Apply yourself where a site blocks automated browsers or needs its own account (for example Workday). The **Quick apply kit** has everything ready to copy |
| Reads your mailbox for replies (read-only) | Go to the interviews |

## What you need

- **A computer that stays on** while you want Sarthi to search: Windows 10/11, macOS or Linux, with 8 GB
  of memory. Docker needs about 4 GB of free disk. Without Docker, plan for about 1.5 GB.
- **One AI key.** Free ones work and take two minutes to get: [Gemini](https://aistudio.google.com/apikey),
  [Groq](https://console.groq.com/keys) or [OpenRouter](https://openrouter.ai/keys). A paid key (DeepSeek,
  OpenAI, Anthropic, Mistral) is faster and more reliable. See [AI models](#ai-models-and-what-they-cost).
- **Your resume** as a PDF, Word (.docx), LaTeX (.tex) or plain-text file.
- *Optional:* Telegram on your phone for alerts, and an email app password for reply tracking.

## Install

Pick one. Docker is easiest if you already have it. The other way is lighter.

### Option A: Docker (Windows, Mac, Linux)

Install [Docker Desktop](https://docs.docker.com/get-docker/) and start it. Then, in a terminal:

```bash
git clone https://github.com/tech-shubh7/Sarthi.git
cd Sarthi
cp .env.example .env      # Windows PowerShell: copy .env.example .env
```

Open `.env` in any text editor. Paste your AI key after the `=` on its line, for example
`SARTHI_GEMINI_API_KEY=your-key`, and save. Then:

```bash
docker compose up -d      # the first start builds Sarthi: about 10 minutes and 3.5 GB
```

Open **http://127.0.0.1:8765** in your browser. That is Sarthi's dashboard.

- To stop it, run `docker compose down`. Your data stays in the `data` folder.
- After you change `.env`, run `docker compose up -d` again. A plain `restart` does not re-read `.env`.
- Under Docker, login windows for job sites open at **http://127.0.0.1:6080**.
- On Linux the container runs as user id 1000. If `id -u` prints another number, run
  `sudo chown -R 1000:1000 data` once.

### Option B: Without Docker

You need [Python 3.12](https://www.python.org/downloads/) and
[uv](https://docs.astral.sh/uv/getting-started/installation/). On **Windows**, install
[WSL](https://learn.microsoft.com/windows/wsl/install) (Ubuntu) and run these commands inside it. Login
windows still open on your Windows desktop.

```bash
git clone https://github.com/tech-shubh7/Sarthi.git
cd Sarthi
uv sync                                # installs Sarthi and what it needs (a few minutes)
uv run playwright install chromium     # the browser Sarthi fills forms in
                                       # (Linux/WSL, first time: add --with-deps; it asks for your password)
cp .env.example .env                   # then paste one AI key into .env, as above
uv run sarthi run                      # leave this running
```

Open **http://127.0.0.1:8765**. Stop Sarthi with **Ctrl+C**. After you change `.env`, stop it and run
`uv run sarthi run` again. `uv run sarthi --help` lists every command.

## First-time setup, step by step

1. **Add your profile.** On the dashboard, go to **Settings → Profile** and fill in:
   - your resume file;
   - the job titles you want (for example *Backend Engineer, Python Developer*) and your skills, as they
     appear on your resume;
   - the country you live in, whether you want remote work, and any other countries you would move to;
   - your years of work experience (0 for a fresher) and, if you like, a minimum salary.

   Press **Save profile**. Prefer to be asked? Send `/setup` to the Telegram bot and upload your resume: it
   suggests job titles and skills for you to confirm.
2. **Log in to the job sites you use.** Go to **Settings → Job-site logins** and press a site (LinkedIn,
   Naukri, Indeed, ...).
   - A browser window opens on that site's own login page. Log in as you normally do.
   - Tick *Keep me signed in* if the site offers it.
   - Wait until you see your account page, then close the window.

   Sarthi keeps only the site's session, never your password. If a site later logs you out, it appears
   under **Applications → Login needed**.
3. **Choose how much Sarthi may do alone.** By default it fills forms and waits for your **Submit**.
   - To let it apply by itself, set an auto-apply line (for example 85%) in **Settings → Automation**.
   - To let it send on job boards too, run `/autoapply boards on` in Telegram, or set
     `SARTHI_AUTO_APPLY_JOB_BOARDS=true` in `.env`.

   See [Applying and auto-apply](#applying-and-auto-apply).
4. *(Optional)* **Connect Telegram.** Go to **Settings → Telegram** and press *Connect Telegram*. In
   Telegram, tap **Create** and then **Start**. Sarthi makes a bot that is yours alone. You do not need
   @BotFather or a token ([how it works](deploy/telegram-connect/README.md)).
5. *(Optional)* **Track replies.** Go to **Settings → Email**. Enter your mail server and an *app
   password*, not your normal password. For Gmail, make one at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Press *Test connection*.
6. **Wait for matches.** Sources start at once. The first matches usually show on **Matches** within an
   hour.

## Using it every day

### The dashboard (http://127.0.0.1:8765)

Only your own computer can open it. No one else on your network can.

| Page | What it is for |
|---|---|
| **Overview** | Everything at a glance: jobs found, matches, applications, and what is waiting for you |
| **Matches** | Every match from the last week, with its score, what fits and what is missing. **Apply**, **Later** or **Skip** |
| **Applications → Needs you** | Questions a form asked, and filled forms waiting for your **Submit** (with a screenshot) |
| **Applications → Solve CAPTCHA** | Forms that stopped at a CAPTCHA. **Finish it myself** opens the filled form. Solve the CAPTCHA and press Submit |
| **Applications → Login needed** | Sites that showed you as signed out. Log in once and the waiting jobs carry on |
| **Applications → Apply yourself** | Jobs Sarthi cannot send. Each one has a **Quick apply kit**. After you apply, press **I applied** |
| **Applications → Verify submission** | Submit was pressed but the site showed no confirmation. Check your email or the site |
| **Applications → In progress / Submitted / Closed** | What is being worked on, what was sent (with replies), and what closed |
| **Activity** | A timeline of everything Sarthi did, form questions, and auto-applies you can still cancel |
| **Sources** | Which job sources work, and which sites and companies the jobs came from |
| **Settings** | Profile, automation, job-site logins, email, Telegram |

### Telegram (optional)

Every job card has **Apply**, **Skip**, **Later**, **Details** and **Tailored resume** buttons. You can also
type a change in plain words, for example *add UAE to my countries*.

| Command | What it does |
|---|---|
| `/setup` | Create or redo your profile from your resume |
| `/profile` | Show your profile |
| `/today` | The best matches you have not seen yet |
| `/catchup` | Everything posted in the last 1 to 7 days, in one batch |
| `/status` | What Sarthi has found and sent so far |
| `/pause` · `/resume` | Stop or restart instant alerts (matches still wait for you) |
| `/login` | Log in once to LinkedIn, Naukri, Indeed and other sites |
| `/autoapply 85` · `/autoapply off` | Auto-apply from 85% (75 to 100), or turn it off |
| `/autoapply boards on` · `off` | Let auto-apply also send on job boards where you are logged in |
| `/autoapply taps on` · `off` | Send a job you tapped Apply on without a second Submit tap |
| `/help` | What Sarthi does, and these commands |

The dashboard and Telegram show the same things. An answer given in one shows in the other.

## Where the jobs come from

All of these are searched for **your** job titles and countries. You do not have to pick them.

| Source | What it covers |
|---|---|
| **LinkedIn, Indeed** | Every country. Searched every hour |
| **Country boards** | Naukri (India), Bayt (Gulf and Middle East), Bdjobs (Bangladesh) and HelloWork (France). Added automatically for your country |
| **Freehire** | One search over ~430 job sites and hiring systems: SEEK, Reed, USAJOBS, Workday, Oracle, SuccessFactors, national job portals and more |
| **Company career pages** | A daily snapshot of ~8 million open jobs at ~192,000 companies, from [ATS-Scrapers](https://github.com/kalil0321/ats-scrapers). It covers Greenhouse, Lever, Ashby, Workday, SmartRecruiters and 90 more hiring systems. Sarthi reads it again each time it changes, about once a day |
| **Free feeds** | Remotive, RemoteOK, We Work Remotely, Himalayas, Jobicy, Arbeitnow, Working Nomads, Hacker News "Who is hiring" |
| **Adzuna, Jooble** | Large job aggregators, once you add their free keys to `.env` |
| **Companies you name** | `SARTHI_WATCH_COMPANIES=["greenhouse/razorpay"]` checks those boards every 30 minutes |

When the same job appears on several sites, Sarthi merges it into one. The dashboard's **Sources** page
shows each source's health and how many jobs it brought.

## How matching works

Each job goes through two checks.

1. **Hard rules, with no AI.** These hide a job when:
   - it is in a country you did not choose, or needs a visa you do not have;
   - it asks for far more years than you have. Sarthi allows 1 year more early in a career, and 2 later;
   - its title is a level above you (Senior, Lead, Staff, Manager, SDE III, ...);
   - its duties belong to a senior person (setting the roadmap, mentoring, leading design reviews) and you
     have less than 3 years. This applies even when the title says nothing about level;
   - it looks like a scam, for example it asks for a training fee or a deposit.
2. **An AI check of the must-haves.** The AI reads the posting and checks each required skill against your
   resume. The score is how many must-haves your resume really shows, not how many words the two share.
   A job for a different role gets a low score, however many keywords match.

Every match lists ✓ what you have and ✗ what is missing. Strong matches are sent at once, at most a few an
hour. The others wait for the daily digest, at 7 pm by default. The score a job needs to be sent at once
changes over time with what you apply to and what you skip.

**Did a job score too low?** Open it and read the ✗ lines. If your resume really shows that skill, add it in
**Settings → Profile**.

## Your resume and cover letter

- **Your resume is sent exactly as you uploaded it** by default: your design, your links, your words.
- **A cover letter is written for each job** you apply to. It uses your real experience and what that job
  needs. It is checked so it never claims something your resume does not say.
- **Tailored resumes are experimental and off by default.** Set `SARTHI_TAILOR_RESUME=true` to turn them
  on.
  - The AI rewords some resume lines toward each job.
  - A changed line is kept only if your original resume backs it.
  - The result is a new PDF in a clean layout, with all your contact details and links.

  They are off by default because one wrong line costs you more than a tailored one gains.

Every document is on the job's page under **Applications**. In Telegram, tap **Tailored resume**.

## Applying and auto-apply

**When you press Apply:**

1. Sarthi writes the letter and fills the form in a hidden browser.
2. If the form asks something your profile cannot answer, Sarthi asks you once, on **Activity** or in
   Telegram. It remembers your answer for later forms.
3. A screenshot of the filled form appears on **Applications → Needs you**. Press **Submit** to send it, or
   **Cancel**.

**Auto-apply is off until you turn it on.** Set a line in **Settings → Automation**, or send
`/autoapply 85` in Telegram. Then:

- Each new match at or above the line is announced, for example: *"I'll apply to Backend Engineer at Acme
  in 5 minutes unless you cancel."* You can cancel it on **Activity** or in Telegram.
- After 5 minutes Sarthi fills the form. It **sends the form by itself only when every answer came from
  your profile or your saved answers**. Any other form comes back to you as a normal preview.
- On **job boards** (LinkedIn, Naukri, Indeed, ...) it sends by itself only after you turn on
  `/autoapply boards on`. This is off by default because these sites can restrict accounts that apply in
  bulk.
- **Limits:**
  - at most 30 applications per site per day (`SARTHI_APPLY_DAILY_LIMIT`);
  - an optional daily cap just for auto-apply (`SARTHI_AUTO_APPLY_DAILY_LIMIT`);
  - never a second application to the same job.

## When Sarthi needs you

Some steps a program should not do, or cannot do. Each kind has its own list on the **Applications** page,
with a count in the sidebar:

| What happened | What you do |
|---|---|
| **A CAPTCHA** ("I'm not a robot") | Press **Finish it myself**. The filled form opens in a window on your computer. Solve the CAPTCHA, press Submit there, and close the window |
| **The site blocks automated browsers**: a "Request blocked" or "Verify you are human" page (often Indeed and Naukri) | The job moves to **Apply yourself**. Open its **Quick apply kit**: the job link, your resume and letter to download, and your details and saved answers to copy. Apply in your own browser, then press **I applied** |
| **The site needs its own account** (Workday and similar) | The same: **Apply yourself**, with the Quick apply kit |
| **You were logged out** of a job site | **Login needed**: log in once. Every job waiting for that site continues |
| **No confirmation after Submit** | **Verify submission**: check the site or your email before you try again |

Sarthi never solves CAPTCHAs, never hides that it is a program, and never tries to get around a site's
protection.

## AI models and what they cost

Sarthi needs at least one AI key. It tries the models listed in `.env` in order. When one is busy or has
used up its free quota, the next one answers. Models without a key are skipped.

| Kind | Examples | Cost | Notes |
|---|---|---|---|
| Free cloud tiers | Gemini, Groq, OpenRouter `:free` models | Free | They have daily limits. When the limits run out, matching waits for the next day |
| Paid APIs | DeepSeek, OpenAI, Anthropic, Mistral | Pay per use | Faster and steadier. `SARTHI_LLM_DAILY_TOKEN_BUDGET` caps what paid models may use (default 500,000 tokens a day) |
| On your computer | Ollama | Free | For writing documents only. Matching never runs on a local model, so your laptop stays usable |

```ini
# in .env: a paid model first, free ones after it
SARTHI_LLM_FAST_MODELS=["deepseek/deepseek-flash","gemini/gemini-3.5-flash-lite"]
SARTHI_LLM_QUALITY_MODELS=["deepseek/deepseek-flash","gemini/gemini-3.5-flash"]
SARTHI_DEEPSEEK_API_KEY=your-key
SARTHI_GEMINI_API_KEY=your-key
```

*Fast* models score jobs, which takes many small calls. *Quality* models write your letters. Before any text
goes to an AI model, Sarthi removes your email, phone number and links. Free tiers may keep what they
receive, so use a paid key or a local model if that matters to you.

## Settings

All settings live in `.env`. [.env.example](.env.example) lists every option with a note. Most people only
change these:

| Setting | Default | What it does |
|---|---|---|
| `SARTHI_GEMINI_API_KEY` (or another provider's key) | empty | Your AI key. One is enough |
| `SARTHI_JOB_BOARDS` | `["linkedin","indeed"]` | Job boards searched every hour. Your country's boards are added automatically |
| `SARTHI_FREEHIRE` | `true` | The ~430-site search |
| `SARTHI_FEEDS` | all free feeds | Remote-job feeds. `[]` turns them off |
| `SARTHI_WATCH_COMPANIES` | `[]` | Extra company boards to watch, for example `["greenhouse/razorpay","lever/acme"]` |
| `SARTHI_DIGEST_HOUR` | `19` | When the daily digest arrives (local time) |
| `SARTHI_APPLY_DAILY_LIMIT` | `30` | Applications per site per day |
| `SARTHI_AUTO_APPLY_MIN_SCORE` | off | Auto-apply line (75 to 100). Also in Settings → Automation |
| `SARTHI_AUTO_APPLY_JOB_BOARDS` | `false` | Let auto-apply send on LinkedIn, Naukri, Indeed and other job boards |
| `SARTHI_AUTO_APPLY_WAIT_MINUTES` | `5` | How long you have to cancel an auto-apply |
| `SARTHI_TAILOR_RESUME` | `false` | Reword resume lines for each job (experimental) |
| `SARTHI_LLM_DAILY_TOKEN_BUDGET` | `500000` | Daily limit for paid AI models (0: no limit) |
| `SARTHI_DASHBOARD_PORT` | `8765` | The dashboard's port |
| `SARTHI_TELEGRAM_CONNECT_URL` | Sarthi's connector | Set it empty to turn Telegram off completely |

Changes you make on the dashboard (profile, logins, email) work without a restart. A change to `.env`
needs a restart.

## Privacy and safety

- **Local first.** Everything Sarthi keeps is in the `data` folder: the database, your resume, the
  documents and the site logins. The dashboard listens on 127.0.0.1 only.
- **No passwords.** You log in on each site's own page, and Sarthi keeps only the session. Email uses an
  app password you can revoke.
- **No made-up claims.** Letters and tailored lines are checked against your own resume.
- **No CAPTCHA solving and no bot-detection tricks.** When a site wants a person, Sarthi gives the step to
  you.
- **Limits on purpose.** Each site has a daily limit, and Sarthi never applies to the same job twice.
- **Your data, your choice.** Delete the `data` folder and everything is gone.

Found a security problem? Please follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Troubleshooting and FAQ

**The dashboard does not open.**
- Check that Sarthi is still running: the `uv run sarthi run` terminal is still open, or
  `docker compose ps` shows it as *running*.
- Wait a minute after starting.
- If port 8765 is taken, set `SARTHI_DASHBOARD_PORT=8770` in `.env` and open that port instead.

**No matches after an hour.**
- Open **Sources** to see whether jobs are being found.
- If they are, check that your job titles are the ones employers use (*Backend Engineer*, not *coder*).
- Check that your countries, or your choice of remote jobs, are set.
- If **Overview** says the AI is busy, add a second AI key.

**"The AI is busy" or "out of quota".** Free tiers have per-minute and per-day limits. Add a key for a
second provider, or wait. Jobs are matched once a model answers again.

**A site keeps showing me as logged out.** Log in again from **Settings → Job-site logins**. Tick *Keep me
signed in*, and wait for your account page before you close the window. Some sites end sessions after a few
days. Sarthi then lists them under **Login needed**.

**Why are so many jobs under "Solve CAPTCHA" or "Apply yourself"?** Some sites show every automated browser
a CAPTCHA, or block it outright. Sarthi does not fight that. It fills what it can and leaves the last step to
you. With the Quick apply kit, that takes about a minute per job.

**Will it spam companies?** No. Nothing is sent without your Submit unless you turn on auto-apply.
Auto-apply has a cancel window and per-site limits, and it never applies to the same job twice.

**Can I use it without Telegram?** Yes. The dashboard does everything. Telegram only adds alerts on your
phone.

**Does it work outside India, or outside tech?** Yes. Matching, documents and forms work for any country and
any kind of job. The tests include profiles from many countries, and from nursing, teaching, finance, sales,
design, trades and more. How many jobs you get depends on your country. LinkedIn, Indeed, Freehire and the
company snapshot cover most of the world.

**How do I update Sarthi?** Run `git pull`. Then, with Docker, run `docker compose up -d --build`. Without
Docker, run `uv sync` and start Sarthi again. Your `data` folder is kept.

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

[AGPL-3.0-only](LICENSE). You may use, change and share Sarthi freely. If you run a modified version as a
service for others, you must share your changes under the same licence.
