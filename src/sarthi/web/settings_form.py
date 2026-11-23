"""The profile as a web form, and a submitted form back to a profile, read as the Telegram setup reads it.

The same parsers as the chat (``sarthi.profile.answers``), so "12 LPA", "UK, UAE", "none" or "1.5 years" mean
the same here as there. Nothing is saved from here: the app saves what ``profile_from_form`` returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from sarthi.jobs.geo import country_name
from sarthi.profile.answers import (
    is_none,
    is_salary_skip,
    parse_countries,
    parse_salary,
    parse_years,
    split_list,
)
from sarthi.profile.models import Preferences, Profile, RemotePreference

MAX_FIELD_CHARS = 2000
FIELDS = (
    "roles", "home", "countries", "visa_free", "remote", "years", "skills", "salary", "exclude_companies",
    "looking_for", "ruled_out",
)  # fmt: skip
REMOTE_CHOICES = {
    RemotePreference.OK.value: "Remote, hybrid or on-site",
    RemotePreference.ONLY.value: "Remote only",
    RemotePreference.NO.value: "No remote (on-site or hybrid)",
}


@dataclass
class FormResult:
    """A profile when the form was valid, else the problems (field -> message) and the typed values."""

    profile: Profile | None
    errors: dict[str, str] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)


def _names(codes: frozenset[str] | set[str]) -> str:
    return ", ".join(sorted(country_name(c) for c in codes))


def form_values(profile: Profile | None) -> dict[str, str]:
    """The profile as the form's text fields (empty for a first setup)."""
    if profile is None:
        return {"remote": RemotePreference.OK.value}
    p = profile.preferences
    salary = p.min_salary
    return {
        "roles": ", ".join(p.roles),
        "home": country_name(p.home_country),
        "countries": _names(p.countries) or "none",
        "visa_free": _names(p.authorized_countries) or "none",
        "remote": p.remote.value,
        "years": f"{p.years_experience:g}" if p.years_experience is not None else "",
        "skills": ", ".join((*profile.skills, *p.keywords)),
        "salary": f"{salary.currency} {salary.minimum:,.0f} / {salary.period.value}"
        if salary is not None and salary.minimum
        else "",
        "exclude_companies": ", ".join(p.exclude_companies),
        "looking_for": p.looking_for,
        "ruled_out": p.ruled_out,
    }


def _countries(text: str, home: str, errors: dict[str, str], key: str) -> frozenset[str]:
    if not text.strip() or is_none(text):
        return frozenset()
    found = parse_countries(text)
    if found.unknown:
        errors[key] = f"Not a country I know: {', '.join(found.unknown[:3])}"
    return found.codes - {home}


def _kept(current: Profile | None) -> dict[str, Any]:
    """Preferences the form does not show (set in the chat): kept as they are."""
    if current is None:
        return {}
    p = current.preferences
    return {
        "employment_types": p.employment_types,
        "seniority": p.seniority,
        "exclude_keywords": p.exclude_keywords,
    }


def profile_from_form(form: Mapping[str, str], current: Profile | None, resume_text: str = "") -> FormResult:
    """The edited profile, or the problems. ``resume_text`` replaces the resume when given."""
    values = {key: str(form.get(key, ""))[:MAX_FIELD_CHARS].strip() for key in FIELDS}
    errors: dict[str, str] = {}

    roles = tuple(r for r in split_list(values["roles"]) if r)[:12]
    if not roles:
        errors["roles"] = "Add at least one job title, e.g. Backend Engineer, AI Engineer."
    home_codes = parse_countries(values["home"]).codes if values["home"] else frozenset()
    if len(home_codes) != 1:
        errors["home"] = "Write the one country you live in, e.g. India."
    home = next(iter(home_codes)) if len(home_codes) == 1 else "IN"
    countries = _countries(values["countries"], home, errors, "countries")
    visa_free = _countries(values["visa_free"], home, errors, "visa_free")
    remote = values["remote"] if values["remote"] in REMOTE_CHOICES else ""
    if not remote:
        errors["remote"] = "Pick one."
    years = parse_years(values["years"]) if values["years"] else None
    if values["years"] and years is None:
        errors["years"] = "Write years like 1, 1.5 or 2 years (0 for a fresher)."
    salary = None
    if values["salary"] and not is_salary_skip(values["salary"]):
        salary = parse_salary(values["salary"], home)
        if salary is None:
            errors["salary"] = "Write it like 6 LPA, 80k GBP or USD 120000, or leave it empty."
    skills = tuple(s for s in split_list(values["skills"]) if s)[:60]
    excluded = tuple(c for c in split_list(values["exclude_companies"]) if c)[:50]
    if errors:
        return FormResult(None, errors, values)
    try:
        preferences = Preferences(
            roles=roles,
            home_country=home,
            countries=countries,
            authorized_countries=visa_free,
            remote=RemotePreference(remote),
            years_experience=years,
            min_salary=salary,
            exclude_companies=excluded,
            looking_for=values["looking_for"],
            ruled_out=values["ruled_out"],
            **_kept(current),
        )
        profile = Profile(
            preferences=preferences,
            skills=skills,
            resume_text=resume_text or (current.resume_text if current else ""),
        )
    except ValidationError as error:
        problem = error.errors()[0]
        return FormResult(None, {"form": f"{'.'.join(map(str, problem['loc']))}: {problem['msg']}"}, values)
    return FormResult(profile, {}, values)
