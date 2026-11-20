"""Versioned schema changes, applied in order on every start.

The applied version is kept in SQLite's ``user_version``. Never edit a released migration: append a new one.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[str, ...] = (
    # 1: jobs, the boards each job was seen on, and the task queue
    """
    CREATE TABLE job (
        id                   TEXT PRIMARY KEY,          -- sha1 of the canonical URL first seen
        role_key             TEXT,                      -- company|role, joins reposts across boards
        canonical_url        TEXT NOT NULL UNIQUE,
        source               TEXT NOT NULL,             -- source of the first sighting
        title                TEXT NOT NULL,
        company              TEXT NOT NULL,
        country_codes        TEXT NOT NULL DEFAULT '',  -- space-separated ISO codes, e.g. ' IN GB '
        remote_type          TEXT NOT NULL,
        posting              TEXT NOT NULL,             -- the full JobPosting as JSON
        posted_at            TEXT,
        first_seen_at        TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL
    ) STRICT;
    CREATE INDEX job_role_key ON job (role_key, first_seen_at);
    CREATE INDEX job_first_seen ON job (first_seen_at);

    CREATE TABLE job_source (
        job_id               TEXT NOT NULL REFERENCES job (id) ON DELETE CASCADE,
        source               TEXT NOT NULL,
        canonical_url        TEXT NOT NULL UNIQUE,
        first_seen_at        TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL,
        PRIMARY KEY (job_id, canonical_url)
    ) STRICT;

    CREATE TABLE task (
        id                   INTEGER PRIMARY KEY,
        type                 TEXT NOT NULL,
        key                  TEXT NOT NULL,             -- idempotency key, e.g. a job id
        payload              TEXT NOT NULL DEFAULT '{}',
        status               TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'running', 'done', 'failed')),
        attempts             INTEGER NOT NULL DEFAULT 0,
        priority             INTEGER NOT NULL DEFAULT 0, -- higher runs first
        run_after            TEXT NOT NULL,
        started_at           TEXT,                      -- set on claim; finds tasks stuck after a crash
        rerun                INTEGER NOT NULL DEFAULT 0, -- re-enqueued while running: run once more after
        last_error           TEXT,
        UNIQUE (type, key)
    ) STRICT;
    CREATE INDEX task_ready ON task (status, priority DESC, run_after);
    """,
    # 2: per-source state: first-run baseline (no alert flood) and health
    """
    CREATE TABLE source_state (
        name                 TEXT PRIMARY KEY,
        baseline_done        INTEGER NOT NULL DEFAULT 0, -- first successful read only records what is there
        last_checked_at      TEXT,
        last_ok_at           TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error           TEXT
    ) STRICT;
    """,
    # 3: match results, one per job, re-run when the profile changes
    """
    CREATE TABLE job_match (
        job_id               TEXT PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
        profile_version      TEXT NOT NULL,
        score                INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
        decision             TEXT NOT NULL CHECK (decision IN ('hide', 'digest', 'alert')),
        rule                 TEXT NOT NULL,
        hidden_because       TEXT,
        reasons              TEXT NOT NULL DEFAULT '[]',  -- JSON list of short lines
        notes                TEXT NOT NULL DEFAULT '[]',
        ai_used              INTEGER NOT NULL DEFAULT 0,
        matched_at           TEXT NOT NULL
    ) STRICT;
    CREATE INDEX job_match_decision ON job_match (decision, matched_at);
    """,
    # 4: full job text for the AI (compressed), AI answer cache, AI token usage for the daily budget
    """
    CREATE TABLE job_text (
        job_id               TEXT PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
        text_zlib            BLOB NOT NULL
    ) STRICT;

    CREATE TABLE llm_cache (
        key                  TEXT PRIMARY KEY,          -- sha256 of model, prompt version and input
        value                TEXT NOT NULL,
        created_at           TEXT NOT NULL
    ) STRICT;

    CREATE TABLE llm_usage (
        id                   INTEGER PRIMARY KEY,
        provider             TEXT NOT NULL,
        model                TEXT NOT NULL,
        prompt_tokens        INTEGER NOT NULL DEFAULT 0,
        completion_tokens    INTEGER NOT NULL DEFAULT 0,
        at                   TEXT NOT NULL
    ) STRICT;
    CREATE INDEX llm_usage_at ON llm_usage (at);
    """,
)


def schema_version(conn: sqlite3.Connection) -> int:
    """The number of migrations applied to this database."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration, each in its own transaction. Returns the new version.

    Raises ``RuntimeError`` when the database is newer than this code (a downgrade), to avoid corrupting it.
    """
    current = schema_version(conn)
    if current > len(MIGRATIONS):
        raise RuntimeError(f"database schema v{current} is newer than this Sarthi (v{len(MIGRATIONS)})")
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        # executescript commits any open transaction first, so the BEGIN/COMMIT live inside the script.
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)
