# Cases ported from JobRadar (MIT), tests/test_links.py and tests/test_dedupe.py @ fdd739b,
# plus Sarthi's own additions.
import pytest

from sarthi.jobs.dedupe import fingerprint, job_id_for_url, norm, normalise_url, role_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://WWW.Acme.com/Jobs/42/?utm_source=x&b=2&a=1#apply", "https://acme.com/Jobs/42?a=1&b=2"),
        ("https://acme.com/?fbclid=1&gclid=2&ref=tg&trk=3&si=4", "https://acme.com"),
        ("http://acme.com:80/x", "http://acme.com/x"),
        ("https://acme.com:8443/x", "https://acme.com:8443/x"),
        ("acme.com/careers", "https://acme.com/careers"),
        ("ftp://acme.com/x", None),
        ("https://localhost/x", None),
        ("https://[bad", None),
        # Sarthi additions: job-system tracking parameters are dropped, job ids are kept
        (
            "https://boards.greenhouse.io/acme/jobs/123?gh_src=abc&gh_jid=123",
            "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123",
        ),
        (
            "https://jobs.lever.co/acme/uuid?lever-source=LinkedIn&lever-origin=applied",
            "https://jobs.lever.co/acme/uuid",
        ),
    ],
)
def test_normalise_url(raw: str, expected: str | None) -> None:
    assert normalise_url(raw) == expected


def test_job_id_is_stable_sha1() -> None:
    assert job_id_for_url("https://acme.com/j/1") == job_id_for_url("https://acme.com/j/1")
    assert job_id_for_url("https://acme.com/j/1") != job_id_for_url("https://acme.com/j/2")
    assert len(job_id_for_url("https://acme.com/j/1")) == 40


def test_norm_and_fingerprint() -> None:
    assert norm("Acme Pvt. Ltd. is Hiring!") == "acme"
    assert fingerprint("ACME Private Limited", "Software Engineer - I", "Bengaluru, KA") == (
        "acme|software engineer i|bengaluru ka"
    )
    assert fingerprint(None, "SDE", "Pune") is None


def test_role_key_joins_the_same_role_across_cities_and_suffixes() -> None:
    assert role_key("Acme GmbH", "Backend Engineer") == role_key("ACME", "Backend Engineer!")
    assert role_key("Acme Pte. Ltd.", "Backend Engineer") == "acme|backend engineer"
    assert role_key("Acme", "") is None
    assert role_key("", "Backend Engineer") is None
