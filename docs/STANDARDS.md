# Sarthi — Engineering & Security Standards

Every change must follow these rules. CI enforces everything marked **(enforced)**.

## 1. Code quality

| Rule | How |
|---|---|
| Python 3.12+, `src/` layout | `pyproject.toml` |
| Formatting + linting: pycodestyle, pyflakes, bugbear, pylint, naming, pathlib, docstrings, no `print`, no commented-out code, no bare `except` | `ruff check` + `ruff format` **(enforced)** |
| Strict typing on every function | `mypy --strict` **(enforced)** |
| Architecture boundaries (§3) | `lint-imports` **(enforced)** |
| Tests for every module, ≥ 80% coverage | `pytest --cov-fail-under=80` **(enforced)** |
| Public functions and modules have docstrings (Google style) | ruff `D` **(enforced)** |

Principles:
- **Single responsibility:** one module = one job. Over ~400 lines → split.
- **Small functions**, explicit names, no magic numbers (constants at module top).
- **Pure core, thin edges:** parsing, scoring and decisions are pure functions; network, DB and browser stay at the edges and are injected, so they can be faked in tests.
- **Typed data, not dicts:** pydantic models at every boundary (source output, DB rows, AI output, chat payloads).
- **Fail loudly inside, gracefully outside:** raise specific exceptions internally; a failing source or AI call never crashes the worker. Log it, retry via the queue, mark the task failed after the retry limit.
- **No speculative code:** no unused abstractions, flags or "might need later" parameters.
- **Async I/O** for network-bound work (sources, AI, browser); never block the event loop.
- **Timezone-aware UTC** datetimes everywhere (ruff `DTZ`).

## 2. Reusing code from other repos

1. Check the repo's licence first (MIT, Apache-2.0, BSD, GPL-3.0 and AGPL-3.0 are allowed; **no licence = do not copy**).
2. Add a header to every copied or ported file:
   ```python
   # Adapted from <repo> (<licence>), <upstream path> @ <commit>
   # Copyright (c) <original authors>. Modifications (c) Sarthi contributors.
   ```
3. Copy the source's LICENSE (and NOTICE, for Apache-2.0) into `licenses/` and list it in `THIRD_PARTY_NOTICES.md`.
4. Copied code must meet these standards before merge (types, tests, lint), and its tests come with it where they exist.
5. Prefer depending on a maintained library (ATS-Scrapers, JobSpy) over copying its code.

## 3. Architecture rules (enforced by import-linter)

- **Pipeline steps never import each other:** `sources`, `matching`, `documents`, `answers`, `apply`, `tracking`, `chat` are independent. They talk only through `db` + `queue`.
- **Layers import downwards only:** `cli` → steps → `queue / ai / jobs / profile` → `db` → `config / log`.
- **Only `db` writes SQL.** Parameterised queries only, never string-built SQL.
- **Only `ai` talks to LLMs.**

## 4. Security (OWASP-aligned)

### Secrets
- Secrets come from env / `.env` only, typed `SecretStr`; `.env` is git-ignored, `.env.example` has empty placeholders.
- Site passwords and session data are **encrypted at rest** (Fernet, key file mode `0600`).
- **Secrets never go into AI prompts, logs, exceptions, screenshots shared outside the machine, or chat messages.**
- Logs pass through `RedactingFilter` (`sarthi.log`).
- gitleaks runs in pre-commit and CI **(enforced)**.

### Untrusted input (job pages, Telegram posts, emails, forms)
- Treat all of it as hostile data.
- **Prompt injection:** job text goes to the AI only inside clear delimiters as data; AI output is validated against a pydantic schema; the AI in matching/writing has **no tools**.
- The **browser agent** is limited to the job's own site, with an allow-list of actions; it can never read local files, other tabs, or email write tools.
- **SSRF:** every user- or source-supplied URL passes the safe-URL guard (http/https only, no private/loopback/link-local IPs).
- **HTML/LaTeX/CSV injection:** escape every inserted value; CSV exports neutralise leading `= + - @`.
- File uploads (resume): size limit, type check by content, parsed in-process with no shell.

### Process & dependencies
- No `shell=True`, no `eval`/`exec`, no `pickle` of untrusted data (ruff `S` rules **(enforced)**).
- Dependencies pinned in `uv.lock`; `pip-audit` in CI **(enforced)**; Dependabot/Renovate for updates.
- Docker image: slim base, **non-root user**, read-only app files, only `data/` writable, no secrets baked in.

### Privacy
- All data stays on the user's machine. No telemetry.
- The AI gets a **compact profile** (skills, experience, preferences), never name/email/phone unless the step needs them (form filling).
- The user can export and delete everything (`sarthi export`, `sarthi wipe`).

### Being a good citizen to job sites
- Per-source rate limits and backoff; honour `429` / `Retry-After`.
- Per-site daily application limits, user-configurable.
- CAPTCHAs are detected and handed to the user, never solved or bypassed.

## 5. Testing

- **Unit tests** for pure logic (fast, no network).
- **Recorded fixtures** for sources: real responses saved once, replayed in tests.
- **Live tests** marked `@pytest.mark.live`, skipped by default, run manually: `uv run pytest -m live`.
- Every bug fix comes with a test that fails before the fix.
- Tests mirror `src/sarthi/` folder structure.

## 6. Git & reviews

- Branch from `main`: `feat/…`, `fix/…`, `chore/…`, `docs/…`.
- **Conventional Commits:** `feat(sources): add Freehire reader`.
- Small PRs, one concern each. CI must be green before merge.
- Never commit `data/`, `.env`, sessions, keys or real resumes.

## 7. Local commands

```bash
uv sync                    # install
uv run pre-commit install  # enable hooks once
uv run ruff check --fix && uv run ruff format
uv run mypy src
uv run lint-imports
uv run pytest
uv run pip-audit --skip-editable
```
