import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sarthi.clock import from_iso, to_iso, utcnow
from sarthi.db import migrations
from sarthi.db.connection import connect, transaction


def test_connect_creates_folder_and_applies_safe_settings(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nested" / "sarthi.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert migrations.schema_version(conn) == len(migrations.MIGRATIONS)
    conn.close()


def test_reconnecting_does_not_reapply_migrations(tmp_path: Path) -> None:
    path = tmp_path / "sarthi.db"
    connect(path).close()
    conn = connect(path)
    assert migrations.schema_version(conn) == len(migrations.MIGRATIONS)
    conn.close()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sarthi.db"
    connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {len(migrations.MIGRATIONS) + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="newer than this Sarthi"):
        connect(path)


def test_failed_migration_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        migrations, "MIGRATIONS", ("CREATE TABLE a (x INTEGER); CREATE TABLE a (y INTEGER);",)
    )
    with pytest.raises(sqlite3.OperationalError):
        connect(":memory:")


def test_transaction_commits_or_rolls_back(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE t (x INTEGER)")
    with transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(ZeroDivisionError), transaction(conn):
        conn.execute("INSERT INTO t VALUES (2)")
        _ = 1 / 0
    assert [row[0] for row in conn.execute("SELECT x FROM t")] == [1]
    assert not conn.in_transaction


def test_task_status_is_constrained(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO task (type, key, status, run_after) VALUES ('t', 'k', 'weird', 'x')")


def test_timestamps_are_fixed_width_and_sortable() -> None:
    early = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    later = early + timedelta(microseconds=1)
    ist = timezone(timedelta(hours=5, minutes=30))
    assert to_iso(early) == "2026-01-02T03:04:05.000000Z"
    assert to_iso(early) < to_iso(later)
    assert to_iso(early.astimezone(ist)) == to_iso(early)
    assert from_iso(to_iso(early)) == early
    assert utcnow().tzinfo is not None
    with pytest.raises(ValueError, match="timezone-aware"):
        to_iso(datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="no timezone"):
        from_iso("2026-01-01T00:00:00")
