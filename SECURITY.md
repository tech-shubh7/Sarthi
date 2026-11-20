# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems.
Report them privately through GitHub's "Report a vulnerability" (Security Advisories) on this repository.
We aim to acknowledge reports within 72 hours.

## Scope

Sarthi runs on the user's own machine and handles sensitive data: resumes, site logins, email access and
Telegram tokens. Issues of particular interest:

- secrets leaking into logs, AI prompts, chat messages or files outside `data/`;
- prompt injection from job pages or emails that makes the agent act outside the job's own site;
- SSRF through user- or source-supplied URLs;
- the browser agent reaching local files, other sites or unapproved submissions.

See [docs/STANDARDS.md](docs/STANDARDS.md) §4 for the security rules the code follows.
