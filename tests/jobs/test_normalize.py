from typing import Any

from sarthi.jobs.models import EmploymentType, JobPosting, RemoteType, Seniority, Sponsorship
from sarthi.jobs.normalize import complete, snippet


def make(**overrides: Any) -> JobPosting:
    data: dict[str, Any] = {
        "source": "linkedin",
        "url": "https://www.linkedin.com/jobs/view/1",
        "title": "Senior Backend Engineer",
        "company": "Acme",
    }
    return JobPosting.model_validate(data | overrides)


def test_fills_country_and_details_from_location_and_text() -> None:
    job = complete(
        make(
            location_text="Bengaluru, Karnataka, India",
            description="Hybrid role. We offer visa sponsorship. Full-time.",
        )
    )
    assert job.country_codes == {"IN"}
    assert job.remote_type is RemoteType.HYBRID
    assert job.visa_sponsorship is Sponsorship.YES
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.seniority is Seniority.SENIOR


def test_remote_in_one_country_is_not_remote_anywhere() -> None:
    job = complete(make(location_text="Remote - India", description="This is a fully remote role."))
    assert job.remote_type is RemoteType.REMOTE
    assert job.remote_countries == {"IN"}
    assert not job.is_remote_anywhere


def test_remote_worldwide_stays_open_to_everyone() -> None:
    job = complete(make(description="We are fully remote and hire worldwide. Work from anywhere."))
    assert job.remote_type is RemoteType.REMOTE
    assert job.is_remote_anywhere
    assert job.location_text == "Remote (worldwide)"


def test_a_country_in_the_title_beats_the_boards_worldwide_default() -> None:
    # Seen live on weworkremotely: region "Anywhere in the World", title limited to Bulgaria.
    job = complete(
        make(
            title="Software Engineer (Remote Eligible in Bulgaria)",
            location_text="Anywhere in the World",
            remote_type=RemoteType.REMOTE,
        )
    )
    assert job.remote_countries == {"BG"}
    assert not job.is_remote_anywhere
    assert job.location_text == "Bulgaria"


def test_a_title_part_without_a_place_word_names_no_country() -> None:
    job = complete(
        make(
            title="Backend Engineer (Java)",
            location_text="Anywhere in the World",
            remote_type=RemoteType.REMOTE,
        )
    )
    assert job.is_remote_anywhere
    assert job.location_text == "Anywhere in the World"


def test_source_values_always_win() -> None:
    job = make(
        location_text="London",
        country_codes=["GB"],
        remote_type="onsite",
        visa_sponsorship="no",
        description="Hybrid. Visa sponsorship available. Office in Berlin.",
    )
    filled = complete(job)
    assert (filled.location_text, filled.country_codes) == ("London", {"GB"})
    assert filled.remote_type is RemoteType.ONSITE
    assert filled.visa_sponsorship is Sponsorship.NO
    assert filled.seniority is Seniority.SENIOR  # was unknown, so the title fills it


def test_nothing_to_fill_returns_the_same_object() -> None:
    job = make(title="Developer", description="Great team, great culture.")
    assert complete(job) is job


def test_snippet() -> None:
    assert snippet("  short text ") == ("short text", True)
    text, whole = snippet("word " * 300, limit=50)
    assert not whole
    assert text.endswith("…")
    assert len(text) <= 51
