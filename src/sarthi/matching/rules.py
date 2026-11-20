"""Layer 1 of the match funnel: hard yes/no rules, decided in code before any AI call.

Unknown facts never hide a job: they pass with a note ("visa sponsorship not mentioned"), so the user, or the
AI layer, can judge. Only facts the job states can rule it out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sarthi.jobs.geo import country_name
from sarthi.jobs.models import EmploymentType, JobPosting, RemoteType, Sponsorship
from sarthi.jobs.requirements import min_years_required
from sarthi.jobs.scam import check as scam_check
from sarthi.profile.models import Profile, RemotePreference

LOCATION_FIT_POINTS = 15
LOCATION_UNSURE_POINTS = 9
YEARS_TOLERANCE = 2  # a job asking up to 2 years more than the user has is still worth showing


@dataclass(frozen=True, slots=True)
class RulesResult:
    """Whether the job can be for this user, location points, and notes for the message."""

    eligible: bool
    location_points: int = 0
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    scam_risk: str = "low"


@dataclass
class _Verdict:
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    location_points: int = 0


def _names(codes: frozenset[str] | set[str]) -> str:
    return ", ".join(sorted(country_name(c) for c in codes))


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text) is not None


def _check_remote(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    if prefs.remote is RemotePreference.NO:
        verdict.blockers.append("Remote only, and you want on-site or hybrid work")
        return
    if job.is_remote_anywhere:
        verdict.location_points = LOCATION_FIT_POINTS
        verdict.notes.append("Remote, open worldwide")
    elif prefs.home_country in job.remote_countries:
        verdict.location_points = LOCATION_FIT_POINTS
        verdict.notes.append(f"Remote, open to people in {country_name(prefs.home_country)}")
    elif not job.remote_countries:  # a restriction Sarthi cannot place: a time zone, an unknown city
        verdict.location_points = LOCATION_UNSURE_POINTS
        verdict.notes.append(f"Remote, but limited to “{job.location_text[:80]}”: check it includes you")
    else:
        verdict.blockers.append(f"Remote only for people in {_names(job.remote_countries)}")


def _check_on_site(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    if prefs.remote is RemotePreference.ONLY and job.remote_type in (RemoteType.ONSITE, RemoteType.HYBRID):
        verdict.blockers.append(f"{job.remote_type.value.capitalize()} job, and you want remote only")
        return
    if not job.country_codes:
        verdict.location_points = LOCATION_UNSURE_POINTS
        verdict.notes.append("Location country not stated")
        return
    workable = job.country_codes & prefs.work_countries
    if not workable:
        verdict.blockers.append(f"Located in {_names(job.country_codes)}, outside your countries")
        return
    verdict.location_points = LOCATION_FIT_POINTS
    if workable & prefs.visa_free_countries:
        return
    if job.visa_sponsorship is Sponsorship.NO:
        verdict.blockers.append(f"No visa sponsorship, and you need one for {_names(workable)}")
    elif job.visa_sponsorship is Sponsorship.YES:
        verdict.notes.append("Offers visa sponsorship")
    else:
        verdict.notes.append(f"Visa sponsorship not mentioned (you need one for {_names(workable)})")


def _check_job_terms(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    if (
        job.employment_type is not EmploymentType.UNKNOWN
        and job.employment_type not in prefs.employment_types
    ):
        verdict.blockers.append(f"{job.employment_type.value.replace('_', '-').capitalize()} role")
    needed = min_years_required(job.description)
    if needed is not None and prefs.years_experience is not None:
        if needed > prefs.years_experience + YEARS_TOLERANCE:
            verdict.blockers.append(f"Asks for {needed}+ years of experience")
        elif needed > prefs.years_experience:
            verdict.notes.append(f"Asks for {needed}+ years; you have {prefs.years_experience:g}")


def _check_exclusions(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    company = job.company.lower()
    for excluded in prefs.exclude_companies:
        if excluded.lower() in company:
            verdict.blockers.append(f"Excluded company: {excluded}")
            return
    text = f"{job.title}\n{job.description}".lower()
    for word in prefs.exclude_keywords:
        if _contains_word(text, word):
            verdict.blockers.append(f"Contains excluded keyword: {word}")
            return


def check_rules(job: JobPosting, profile: Profile) -> RulesResult:
    """Apply every hard rule to ``job``."""
    verdict = _Verdict()
    _check_exclusions(job, profile, verdict)
    if job.remote_type is RemoteType.REMOTE:
        _check_remote(job, profile, verdict)
    else:
        _check_on_site(job, profile, verdict)
    _check_job_terms(job, profile, verdict)
    scam = scam_check(job.description)
    if scam.risk == "high":
        verdict.blockers.append("Looks like a scam: " + "; ".join(scam.signals))
    elif scam.risk == "medium":
        verdict.notes.append("Possible scam signs: " + "; ".join(scam.signals))
    return RulesResult(
        eligible=not verdict.blockers,
        location_points=verdict.location_points if not verdict.blockers else 0,
        blockers=tuple(verdict.blockers),
        notes=tuple(verdict.notes),
        scam_risk=scam.risk,
    )
