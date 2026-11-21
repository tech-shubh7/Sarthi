from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from sarthi.queue.tasks import RetryableError
from sarthi.sources.base import SearchQuery
from sarthi.sources.catalog import CatalogSource, build_query, row_to_posting
from tests.conftest import FakeClock

COLUMNS = (
    "url VARCHAR, title VARCHAR, company VARCHAR, ats_type VARCHAR, ats_id VARCHAR, location VARCHAR, "
    "country_iso VARCHAR, is_remote BOOLEAN, workplace_type VARCHAR, salary_min DOUBLE, salary_max DOUBLE, "
    "salary_currency VARCHAR, salary_period VARCHAR, employment_type VARCHAR, apply_url VARCHAR, "
    "description VARCHAR, posted_at VARCHAR, source_created_at TIMESTAMPTZ"
)


def row(
    n: int, title: str, *, country: str | None = "IN", days_ago: int | None = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "url": f"https://boards.greenhouse.io/acme/jobs/{n}",
        "title": title,
        "company": "Acme",
        "ats_type": "greenhouse",
        "ats_id": str(n),
        "location": "Bengaluru, India" if country == "IN" else "Austin, TX",
        "country_iso": country,
        "is_remote": False,
        "workplace_type": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "employment_type": "FULL_TIME",
        "apply_url": None,
        "description": "Build Python APIs. " * 2000,  # longer than kept
        "posted_at": None if days_ago is None else f"DAY{days_ago}",
        "source_created_at": None,
        **extra,
    }


@pytest.fixture
def snapshot(tmp_path: Path, clock: FakeClock) -> Any:
    def make(rows: list[dict[str, Any]]) -> str:
        path = tmp_path / "all.parquet"
        con = duckdb.connect()
        con.execute(f"CREATE TABLE jobs ({COLUMNS})")
        for r in rows:
            values = dict(r)
            if isinstance(values["posted_at"], str) and values["posted_at"].startswith("DAY"):
                day = clock() - timedelta(days=int(values["posted_at"][3:]))
                values["posted_at"] = day.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            placeholders = ", ".join("?" * len(values))  # only "?" marks: the values are parameters
            con.execute(f"INSERT INTO jobs VALUES ({placeholders})", list(values.values()))  # noqa: S608
        con.execute(f"COPY jobs TO '{path}' (FORMAT parquet)")
        con.close()
        return str(path)

    return make


def manifest(*versions: str) -> tuple[httpx.MockTransport, list[int]]:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        version = versions[min(calls[0], len(versions) - 1)]
        calls[0] += 1
        return httpx.Response(200, json={"generated_at": version})

    return httpx.MockTransport(handler), calls


QUERY = SearchQuery(roles=("Backend Engineer", "AI Engineer"), countries=("IN",), hours_old=1)


async def test_the_users_roles_in_their_countries_and_recent_only(snapshot: Any, clock: FakeClock) -> None:
    path = snapshot(
        [
            row(1, "Senior Backend Engineer"),
            row(2, "Backend Engineer", days_ago=10),  # too old
            row(3, "Backend Engineer", country="US"),  # elsewhere
            row(4, "AI Engineer", country=None, location="Pune, India"),  # no country code, place says India
            row(5, "Accountant"),  # not a role of theirs
            row(6, "AI Engineer", ats_type="hiring_system_from_the_future"),  # unknown to this install
            row(7, "Backend Engineer", days_ago=None, source_created_at=clock() - timedelta(hours=5)),
        ]
    )
    transport, _ = manifest("v1")
    jobs = await CatalogSource(
        path, "https://m.example/manifest.json", transport=transport, clock=clock
    ).fetch(QUERY)
    assert sorted(j.source_job_id or "" for j in jobs) == ["1", "4", "7"]
    first = next(j for j in jobs if j.source_job_id == "1")
    assert first.source == "greenhouse"
    assert first.country_codes == frozenset({"IN"})
    assert first.posted_at is not None
    assert first.posted_at.tzinfo is not None
    assert first.description == ""  # descriptions are too big to read safely (see catalog.py)
    assert not first.description_complete


async def test_remote_search_adds_remote_jobs_anywhere(snapshot: Any, clock: FakeClock) -> None:
    path = snapshot(
        [row(1, "AI Engineer", country="US", is_remote=True), row(2, "AI Engineer", country="US")]
    )
    transport, _ = manifest("v1")
    source = CatalogSource(path, "https://m.example/m.json", transport=transport, clock=clock)
    remote = SearchQuery(roles=("AI Engineer",), countries=("IN",), remote=True)
    assert [j.source_job_id for j in await source.fetch(remote)] == ["1"]


async def test_an_unchanged_snapshot_is_not_queried_again(snapshot: Any, clock: FakeClock) -> None:
    path = snapshot([row(1, "Backend Engineer")])
    transport, calls = manifest("v1", "v1", "v2")
    source = CatalogSource(path, "https://m.example/m.json", transport=transport, clock=clock)
    assert len(await source.fetch(QUERY)) == 1
    assert await source.fetch(QUERY) == []  # same snapshot: nothing new, no query
    assert len(await source.fetch(QUERY)) == 1  # a new snapshot: read again (the store drops repeats)
    assert calls[0] == 3


async def test_no_roles_no_query(clock: FakeClock) -> None:
    transport, calls = manifest("v1")
    source = CatalogSource("/nowhere.parquet", "https://m.example/m.json", transport=transport, clock=clock)
    assert await source.fetch(SearchQuery(countries=("IN",))) == []
    assert calls[0] == 0


async def test_a_broken_snapshot_is_tried_again_later(tmp_path: Path, clock: FakeClock) -> None:
    transport, _ = manifest("v1")
    missing = str(tmp_path / "missing.parquet")
    source = CatalogSource(missing, "https://m.example/m.json", transport=transport, clock=clock)
    with pytest.raises(RetryableError, match="catalog"):
        await source.fetch(QUERY)
    with pytest.raises(RetryableError):  # still unread: the next tick tries again, not "nothing new"
        await source.fetch(QUERY)


@pytest.mark.parametrize(
    "role", ["100%_remote dev", "back\\end", "'; DROP TABLE jobs; --", "  Backend   Engineer  "]
)
def test_role_text_is_data_never_sql(role: str, clock: FakeClock) -> None:
    sql, params = build_query(SearchQuery(roles=(role,), countries=("IN",)), clock(), "x.parquet")
    assert role.strip() not in sql
    assert "DROP" not in sql
    assert any("%" in str(p) for p in params[2:])


async def test_wildcards_in_a_role_match_only_themselves(snapshot: Any, clock: FakeClock) -> None:
    path = snapshot([row(1, "Backend Engineer"), row(2, "100% remote dev")])
    transport, _ = manifest("v1")
    source = CatalogSource(path, "https://m.example/m.json", transport=transport, clock=clock)
    jobs = await source.fetch(SearchQuery(roles=("100%",), countries=("IN",)))
    assert [j.source_job_id for j in jobs] == ["2"]  # "%" in the role is a percent sign, not "anything"


def test_unusable_rows_are_skipped() -> None:
    assert row_to_posting({"title": "x"}) is None
    assert (
        row_to_posting({"url": "not a url", "title": "x", "company": "y", "ats_type": "greenhouse"}) is None
    )
