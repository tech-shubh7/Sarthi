# Changelog

All notable changes to Sarthi are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0.0, a minor version may change settings or stored data;
the notes say when.

## [0.1.0] - 2026-11-24

The first public release: a job agent that runs on your own computer, finds jobs that fit you, writes a
tailored resume and cover letter for each, fills the application, and asks you before anything is sent.

### Finding jobs

- Job boards and searches: LinkedIn, Indeed, Naukri and more through JobSpy; Adzuna and Jooble; Remotive,
  RemoteOK, Himalayas and We Work Remotely feeds; Hacker News "Who is hiring"; Freehire.
- Company career pages: a snapshot of about 192,000 company boards (Greenhouse, Lever, Ashby and others).
- Each job's full description is read from its own page; duplicates, reposts and likely scams are dropped.
- A catch-up run after the computer was off (`sarthi catchup --days N`), and a note when a source stops working.

### Matching

- Evidence-based scores: the AI checks the job's must-haves against your resume; a different role, thin
  evidence or a vague posting is capped, so a high score means a real fit.
- Years of experience, level and location are checked against your profile before any AI call.
- At most five instant alerts an hour; the alert line learns from your Apply and Skip taps.

### Documents

- A tailored resume and cover letter (PDF) per job, from your own resume only, with a truth check and an ATS
  keyword report.

### Applying

- Forms filled in a real browser (Greenhouse, Lever, Ashby and other forms; LinkedIn Easy Apply and
  Naukri with your own saved login). Nothing is sent until you tap Submit.
- Optional auto-apply above a score you choose (75 to 100), with 5 minutes to cancel and a daily cap.
- A CAPTCHA is never solved by Sarthi: tap "Finish it myself" and the filled form opens on your computer for
  you to solve it and press Submit; Sarthi records it.
- Questions a form asks are answered from your profile or asked once in Telegram and remembered.

### Telegram and dashboard

- One-tap Telegram setup: a bot of your own, without @BotFather or a token to copy.
- A local dashboard (http://127.0.0.1:8765) to change your profile, alerts and auto-apply, apply, and follow
  every application; it runs without JavaScript and only on your computer.
- Employer replies found in your mailbox, and follow-up drafts for quiet applications.

### Running it

- Docker image and compose file (read-only container, data in `./data`), or `uv run sarthi run`.
- AI from free cloud models (Gemini, OpenRouter, Groq) with a local Ollama model as an option.

[0.1.0]: https://github.com/tech-shubh7/Sarthi/releases/tag/v0.1.0
