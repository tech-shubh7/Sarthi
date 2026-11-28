from datetime import UTC, datetime
from typing import Any

from sarthi.jobs.models import JobPosting
from sarthi.profile.models import Preferences, Profile

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
# A posting long enough for the AI to read (under 300 characters a job is only a title and a line).
POSTING = (
    "Build payment APIs in Python and Go on AWS. 2+ years of backend experience.\n"
    "You will design and ship the services behind checkout, refunds and settlements, write tests for them, "
    "review pull requests with your teammates and keep the services reliable once they are live. "
    "We value clear writing and care for the people who use what we build."
)


def make_job(**overrides: Any) -> JobPosting:
    data: dict[str, Any] = {
        "source": "greenhouse",
        "url": "https://boards.greenhouse.io/razorpay/jobs/1",
        "title": "Backend Engineer",
        "company": "Razorpay",
        "country_codes": ["IN"],
        "remote_type": "hybrid",
        "employment_type": "full_time",
        "description": POSTING,
        "posted_at": datetime(2026, 11, 19, 9, 0, tzinfo=UTC),
    }
    return JobPosting.model_validate(data | overrides)


def make_profile(**overrides: Any) -> Profile:
    data: dict[str, Any] = {
        "roles": ["Backend Engineer"],
        "keywords": ["python", "go", "aws"],
        "home_country": "IN",
        "years_experience": 3,
    }
    return Profile(preferences=Preferences.model_validate(data | overrides), skills=("PostgreSQL",))
