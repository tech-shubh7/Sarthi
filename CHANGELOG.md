# Changelog

All notable changes to Sarthi are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0.0, a minor version may change settings or stored data;
the notes say when.

## [0.1.0] - 2026-11-26

The first public release: a job agent that runs on your own computer, finds jobs that fit you, writes a
tailored resume and cover letter for each in your own resume's design, and fills and sends the application,
with you deciding what may go out on its own.

### Finding jobs

- Job boards and searches: LinkedIn, Indeed, Naukri and other local boards through JobSpy; Adzuna and
  Jooble; Remotive, RemoteOK, Himalayas and We Work Remotely feeds; Hacker News "Who is hiring"; Freehire.
- Company career pages: a snapshot of about 192,000 company boards (Greenhouse, Lever, Ashby and others).
- Each job's full description is read from its own page; duplicates, reposts and likely scams are dropped.
- Works for job seekers in any country: locations, regions, salaries in local currency, local boards per
  country, citizenship or clearance requirements, and languages a job requires are all checked.
- A catch-up run after the computer was off (`sarthi catchup --days N`), and a note when a source stops working.

### Matching

- Evidence-based scores: the AI checks the job's must-haves against your resume; a different role, thin
  evidence or a vague posting is capped, so a high score means a real fit.
- Years of experience, level, location, eligibility and languages are checked before any AI call.

### Documents

- Your uploaded resume is kept and tailored in its own design: in a PDF the changed lines are rewritten in
  place in its own font; .tex and .docx sources are edited in place.
- The same bullets in the same order, in your own words: no new facts, no inflated titles or verbs, no
  phrases copied from the posting. Contact details and profile links are never dropped.
- A cover letter per job and an ATS keyword report.

### Applying

- Forms filled in a real browser: Greenhouse, Lever, Ashby and other hiring systems, any company site
  through an AI that walks the form, and LinkedIn, Naukri and other boards with your own saved login
  (a board job that applies on the company's site goes on to that site).
- You approve each application, or turn on auto-apply above a score you choose (75 to 100), with 5 minutes
  to cancel, optionally on job boards too.
- A CAPTCHA is never solved by Sarthi: "Finish it myself" opens the filled form on your computer for you to
  solve it and press Submit; Sarthi records it.
- Closed postings, pages without a form and sign-in walls are recognised; a sent application is never sent
  twice. Jobs that waited for a site login are filled again once you log in.

### Dashboard and Telegram

- A local dashboard (http://127.0.0.1:8765) does everything: profile, form questions, approvals, auto-apply,
  site logins, mail settings, every application with the resume and letter used, and an Activity feed.
- Telegram is optional: one-tap setup gives you a bot of your own, without @BotFather or a token to copy.
- Employer replies read from your mailbox (read-only, set up from the dashboard), and follow-up drafts for
  quiet applications.

### Running it

- Docker image and compose file (read-only container, data in `./data`), or `uv run sarthi run`.
- AI from free cloud tiers (Gemini, Groq, OpenRouter, Mistral) or low-cost DeepSeek, with a local Ollama
  model as an option.

[0.1.0]: https://github.com/tech-shubh7/Sarthi/releases/tag/v0.1.0
