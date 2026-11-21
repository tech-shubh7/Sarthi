# Third-Party Notices

Sarthi is licensed under AGPL-3.0-only (see `LICENSE`). It includes or adapts code from the projects below.
Each copied or ported file carries a header naming its source. Original licence texts are kept with the
adapted code where the licence requires it.

| Project | Licence | Used for |
|---|---|---|
| [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) / [ibarrajo fork](https://github.com/ibarrajo/ApplyPilot) | AGPL-3.0 | Tailoring, cover letters, tracking, replay paths, Q&A bank |
| [JobRadar](https://github.com/ThePlator/JobRadar) | MIT | Task queue, watchers, liveness, scam, extraction, decide |
| [AIHawk Jobs Applier AI Agent](https://github.com/feder-cr/Jobs_Applier_AI_Agent) | MIT | AI browser apply engine, secret sealing |
| [ATS-Scrapers](https://github.com/kalil0321/ats-scrapers) | MIT | Library dependency + company catalog |
| [JobSpy](https://github.com/speedyapply/JobSpy) | MIT | Library dependency |
| [JobDrop](https://github.com/kbwhodat/jobdrop) | MIT | Library dependency |
| [StillOpen](https://github.com/Druvo/stillopen) | Apache-2.0 | Geo resolution, URL guard, feed connectors, match evidence |
| [Openings](https://github.com/VincenzoImp/openings-job-search) | MIT | Duplicate detection, preferences model |
| [Resume-Matcher](https://github.com/srbhr/Resume-Matcher) | Apache-2.0 | Resume preservation, ATS parse checks |
| [LinkedIn-AI-Job-Applier-Ultimate](https://github.com/beatwad/LinkedIn-AI-Job-Applier-Ultimate) | MIT | LinkedIn Easy Apply flow (`src/sarthi/apply/linkedin.py`) |
| [OpenApply](https://github.com/nturl/openapply) | MIT | Ported: rule-based form adapters, field mapping, truth check, questions, prior-application guard |
| [Career-Ops](https://github.com/career-ops-hq/career-ops) | MIT | Ported: repost detection, UK sponsor check |
| [Freehire](https://github.com/strelov1/freehire) | MIT | Public search API (no code copied) |

This table lists planned sources; an entry is final once code from that project is merged.

When code from a project is merged, its original licence file (and NOTICE file, for Apache-2.0 projects) is
copied into [`licenses/`](licenses/) and listed below. Every adapted file also carries a header naming its source.

## Merged so far

| Project | Licence file | Sarthi files |
|---|---|---|
| JobRadar | [licenses/JobRadar-MIT.txt](licenses/JobRadar-MIT.txt) | `src/sarthi/jobs/dedupe.py`, `src/sarthi/jobs/scam.py`, `src/sarthi/db/tasks.py`, `src/sarthi/queue/tasks.py`, `src/sarthi/queue/worker.py`, `src/sarthi/matching/decide.py`, `src/sarthi/ai/client.py` (structured-call flow), `src/sarthi/ai/prompts.py`, `src/sarthi/sources/freehire.py` |
| ApplyPilot | [licenses/ApplyPilot-AGPL-3.0.txt](licenses/ApplyPilot-AGPL-3.0.txt) | `src/sarthi/ai/client.py` (model fallback chain), `src/sarthi/documents/truth.py` (banned-phrase and AI-leak lists), `src/sarthi/tracking/triage.py`, `src/sarthi/tracking/matcher.py`, `src/sarthi/ai/replies.py` (prompt) |
| OpenApply | [licenses/OpenApply-MIT.txt](licenses/OpenApply-MIT.txt) | `src/sarthi/matching/prefilter.py` (ported from TypeScript), `src/sarthi/documents/truth.py` (truth check), `src/sarthi/jobs/forms.py` (field mapping), `src/sarthi/answers/questions.py` (form questions), `src/sarthi/apply/adapters.py`, `src/sarthi/apply/engine.py`, `src/sarthi/apply/scan.js`, `src/sarthi/apply/captcha.js` (apply engine) |
| StillOpen | [licenses/StillOpen-Apache-2.0.txt](licenses/StillOpen-Apache-2.0.txt), NOTICE: [licenses/StillOpen-NOTICE.txt](licenses/StillOpen-NOTICE.txt) | `src/sarthi/jobs/geo.py`, `src/sarthi/jobs/countries.py`, `src/sarthi/jobs/requirements.py` (pattern), `src/sarthi/sources/feeds.py`, `src/sarthi/sources/hackernews.py`, `src/sarthi/sources/web.py` (`plain_text`) |

## Libraries used at run time (not copied)

- [typst-py](https://github.com/messense/typst-py) (Apache-2.0): PDF rendering of resumes and cover letters.
- [Playwright for Python](https://github.com/microsoft/playwright-python) (Apache-2.0): filling application forms.
- [defusedxml](https://github.com/tiran/defusedxml) (PSF-2.0): safe parsing of RSS feeds.
