"""What to put in each application-form field, decided without AI (docs/ARCHITECTURE.md §2.11, §2.12).

Every answer has a source: the user's saved answers, their profile, or a safe default (a "decline to answer"
option for demographic questions, "Company website" for "how did you hear about us"). A field with no safe
answer is left for the user: it is never guessed. Options are matched to what the form offers; a near miss on a
list where a near miss is a different answer (a degree, a school) is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from sarthi.jobs.experience import experience_answer, norm, same_subject
from sarthi.jobs.geo import location_codes

FieldType = Literal[
    "text", "textarea", "email", "tel", "url", "number", "date", "select", "radio", "checkbox", "combobox",
    "file", "unknown",
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class FormField:
    """One question on an application form."""

    key: str
    label: str
    type: FieldType = "text"
    required: bool = False
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Applicant:
    """What the forms may be filled with. Booleans are None when unknown (the question goes to the user)."""

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    location: str = ""
    country: str = ""  # country name, e.g. "India"
    home_country: str = ""  # ISO code of where the user lives, e.g. "GB"
    authorized_countries: frozenset[str] = frozenset()  # ISO codes the user needs no visa for (home included)
    needs_sponsorship: bool | None = None  # for this job's country
    work_authorized: bool | None = None  # for this job's country
    willing_to_relocate: bool | None = None
    salary_expectation: str = ""
    notice_period: str = ""
    current_company: str = ""
    current_title: str = ""
    school: str = ""
    degree: str = ""
    discipline: str = ""
    start_year: str = ""
    end_year: str = ""
    years_experience: float | None = None  # total years of work experience, from the profile
    skills: tuple[str, ...] = ()  # the skills the resume shows
    saved_answers: dict[str, str] = field(default_factory=dict)  # question -> answer, from earlier forms


@dataclass(frozen=True, slots=True)
class FormContext:
    """Everything a form may be filled from."""

    applicant: Applicant
    resume_file: str = ""
    cover_file: str = ""
    cover_letter: str = ""
    job_location: str = ""
    job_remote: bool = False
    answers: dict[str, str] = field(default_factory=dict)  # this job's own answers (by field key or label)


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A value for a field and where it came from."""

    value: str
    source: Literal["job", "profile", "saved", "default", "file"]


# ---- text similarity ---------------------------------------------------------------------------------------

_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "you",
        "your",
        "are",
        "is",
        "do",
        "to",
        "of",
        "in",
        "for",
        "or",
        "and",
        "if",
        "any",
        "this",
        "that",
        "with",
        "on",
        "at",
        "we",
        "our",
        "us",
        "please",
        "be",
        "have",
        "will",
        "what",
    ]
)
_MATCH_MIN = 0.6


def _tokens(text: str) -> set[str]:
    return {t for t in norm(text).split() if t and t not in _STOP}


def similarity(a: str, b: str) -> float:
    """0..1: exact 1, one contains the other 0.9, else shared words over the larger set."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if (len(na) >= 8 and na in nb) or (len(nb) >= 8 and nb in na):  # noqa: PLR2004
        return 0.9
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


# A question about this one employer ("Why do you want to work here?"): its answer is kept for that job only,
# never offered to another company's form.
_COMPANY_SPECIFIC = re.compile(
    r"\bwhy\b.*\b(?:here|us|our|join|this (?:company|role|team|position|job)|work (?:at|for|with))\b"
    r"|\b(?:interest(?:s|ed)?|excite[sd]?|attract(?:s|ed)?|motivate[sd]?|drew you)\b.*"
    r"\b(?:us|our|this (?:company|role|team|position|job)|about (?:the|this) (?:company|role))\b",
    re.IGNORECASE,
)
_JOB_SCOPE = " [job:"


def company_specific(label: str) -> bool:
    """True for a question about this employer, whose answer must not be reused elsewhere."""
    return bool(_COMPANY_SPECIFIC.search(label))


def for_job(label: str, job_id: str) -> str:
    """The answer bank key of an answer kept for one job only."""
    return f"{label}{_JOB_SCOPE}{job_id}]"


def split_bank(saved: dict[str, str], job_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """(answers for any form, this job's own answers by question) from the whole answer bank."""
    general = {q: a for q, a in saved.items() if _JOB_SCOPE not in q}
    suffix = f"{_JOB_SCOPE}{job_id}]"
    own = {q.removesuffix(suffix): a for q, a in saved.items() if q.endswith(suffix)}
    return general, own


def lookup(answers: dict[str, str], label: str, minimum: float = _MATCH_MIN) -> tuple[str, str] | None:
    """The saved (question, answer) closest to ``label``, or None below ``minimum``."""
    best: tuple[str, str] | None = None
    score = minimum
    for question, answer in answers.items():
        s = similarity(question, label)
        # "years with Python" and "years with Java" share every other word: never each other's answer
        if s >= score and answer != "" and same_subject(question, label):
            best, score = (question, answer), s
    return best


# ---- options ----------------------------------------------------------------------------------------------

_NEG = re.compile(r"\b(no|not|dont|don t|do not|wont|will not|cannot|cant|never|n t)\b")
_DECLINE = re.compile(
    r"decline|prefer not|(dont|do not) (wish|want)|not wish|choose not|rather not|"
    r"not to (say|answer|disclose|self|specify)|no answer"
)


def yes_no_option(options: tuple[str, ...] | list[str], yes: bool) -> str | None:
    """The option meaning yes (or no): "Yes", "I am authorized...", "No, I will not require..."."""
    normalised = [norm(o) for o in options]
    lead = re.compile(r"^yes\b" if yes else r"^no\b")
    for option, n in zip(options, normalised, strict=True):
        if lead.search(n):
            return option
    candidates = [
        o
        for o, n in zip(options, normalised, strict=True)
        if not _DECLINE.search(n) and bool(_NEG.search(n)) != yes
    ]
    return candidates[0] if len(candidates) == 1 else None


def best_option(options: tuple[str, ...] | list[str], value: str, *, strict: bool = False) -> str | None:
    """The option closest to a free-text value; None when nothing is close (never a random pick)."""
    v = norm(value)
    if not v:
        return None
    if re.fullmatch(r"yes|y|true", v):
        return yes_no_option(options, yes=True)
    if re.fullmatch(r"no|n|false", v):
        return yes_no_option(options, yes=False)
    best: str | None = None
    score, tie = 0.5, False
    for option in options:
        n = norm(option)
        s = similarity(option, value)
        if n and (n.startswith(v) or v.startswith(n)):
            s = max(s, 0.85)
        if s > score:
            best, score, tie = option, s, False
        elif best is not None and s == score:
            tie = True
    return None if strict and tie else best


def exact_option(options: tuple[str, ...] | list[str], value: str) -> str | None:
    """The option that reads exactly like the value (case and punctuation aside)."""
    v = norm(value)
    return next((o for o in options if norm(o) == v), None) if v else None


def pick_options(options: tuple[str, ...] | list[str], raw: str) -> list[str]:
    """The options a multi-select answer names ("Career growth, Compensation")."""
    n = norm(raw)
    exact = next((o for o in options if norm(o) == n), None)
    if exact is not None:
        return [exact]
    if not any(re.search(r"[,;|]", o) for o in options):
        picks = [best_option(options, part) for part in re.split(r"\s*[,;|]\s*", raw)]
        return list(dict.fromkeys(p for p in picks if p))
    hay = f" {n} "
    found: list[tuple[int, str]] = []
    for option in sorted(options, key=lambda o: len(norm(o)), reverse=True):
        needle = f" {norm(option)} "
        at = hay.find(needle) if needle.strip() else -1
        if at < 0:
            continue
        found.append((at, option))
        hay = hay[:at] + " " * len(needle) + hay[at + len(needle) :]
    if found and not re.sub(r"\b(and|or)\b", " ", hay).strip():
        return [o for _, o in sorted(found)]
    reworded = best_option(options, raw, strict=True)
    return [reworded] if reworded else []


US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}  # fmt: skip


def best_place(options: tuple[str, ...] | list[str], value: str) -> str | None:
    """The suggestion for a location like "Brooklyn, NY": needs city plus region or country, never a wrong city."""
    direct = best_option(options, value)
    if direct:
        return direct
    code = next((p.strip().upper() for p in value.split(",") if p.strip().upper() in US_STATES), None)
    state = US_STATES.get(code or "")
    want = _tokens(" ".join([value, state or "", "United States USA" if state else ""]))
    best: str | None = None
    score = min(3, len(want)) - 1
    for option in options:
        tokens = _tokens(option)
        if state and code and norm(state) not in norm(option) and code.lower() not in tokens:
            continue
        hit = len(want & tokens)
        if hit > score:
            best, score = option, hit
    return best


# ---- dates ------------------------------------------------------------------------------------------------

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")  # fmt: skip
_COUNT = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}
_SATURDAY, _SUNDAY = 5, 6


def _day(year: int, month: int, day_: int) -> str | None:
    try:
        return date(year, month, day_).isoformat()
    except ValueError:
        return None


# Where 04/11/2026 is April 11 (month first); most of the world writes the day first.
MONTH_FIRST_COUNTRIES = frozenset({"US", "PH", "CA"})
_MAX_MONTH = 12


def to_date(raw: str, today: date | None = None, *, day_first: bool = False) -> str | None:
    """A start-date answer as YYYY-MM-DD, or None when it names no date.

    Takes a date (2026-11-04, 11/04/2026, 24.12.2026, Nov 4 2026, November 4) or a span from today ("1-2 weeks",
    "two weeks' notice", "a month", "immediately"): a span counts its upper bound; a weekend start moves to
    Monday. A numeric date is read month first unless ``day_first`` (or a first number above 12) says otherwise.
    """
    today = today or date.today()  # noqa: DTZ011  (the user's local calendar date)
    s = re.sub(r"\s+", " ", raw.lower().replace(",", " ")).strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        return _day(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{1,2})([/.-])(\d{1,2})\2(\d{4})", s):
        first, second = int(m[1]), int(m[3])
        if first > _MAX_MONTH or (day_first and second <= _MAX_MONTH):
            first, second = second, first
        return _day(int(m[4]), first, second)
    if m := re.fullmatch(r"([a-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", s):
        month = next((i + 1 for i, name in enumerate(_MONTHS) if name.startswith(m[1])), 0)
        if month:
            if m[3]:
                return _day(int(m[3]), month, int(m[2]))
            upcoming = _day(today.year, month, int(m[2]))
            return (
                _day(today.year + 1, month, int(m[2]))
                if upcoming and upcoming < today.isoformat()
                else upcoming
            )
    days: int | None = None
    if re.fullmatch(r"immediately|asap|as soon as possible|right away|now|today", s):
        days = 0
    if m := re.fullmatch(
        r"(?:(?:in|within|about|around|approximately|roughly) )?(\d+|a|an|one|two|three|four|five|six)"
        r"(?: ?(?:-|–|to|or) ?(\d+|two|three|four|five|six))? ?(day|week|month)s?['’]?(?: notice)?",
        s,
    ):
        amount = m[2] or m[1]
        days = (_COUNT.get(amount) or int(amount)) * _UNIT_DAYS[m[3]]
    if days is None:
        return None
    start = today + timedelta(days=days)
    if start.weekday() == _SATURDAY:
        start += timedelta(days=2)
    elif start.weekday() == _SUNDAY:
        start += timedelta(days=1)
    return start.isoformat()


# ---- one field --------------------------------------------------------------------------------------------

_COMMITMENT = re.compile(
    r"relocat|\bon ?site\b|\b(in|at|from|into|to) (the |our |an? )?(\w+ )?office\b|\boffice (role|days?|based)\b|"
    r"\bin person\b|\bcommut|\btravel|\bdays (a|per) week\b|\bhybrid\b"
)
_NOT_PLACE = re.compile(
    r"^(I|I['’]\S*|January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$"
)
_AUTH = re.compile(r"\bsponsor|\bvisa\b|authori[sz]ed|work authori|legally (able|eligible|permitted)|right to work|"
                   r"eligible to work")  # fmt: skip


def places_in(label: str) -> list[str]:
    """Places a question names: runs of capitalised words after each sentence's first word."""
    found: list[str] = []
    for sentence in re.split(r"[.?!:;]\s+", label):
        rest = re.sub(r"^\W*\S+", "", sentence)
        for m in re.finditer(r"[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*)*", rest):
            if not _NOT_PLACE.match(m.group()):
                found.append(m.group())
    return found


def commitment_place(label: str, job_location: str = "", job_remote: bool = False) -> str | None:
    """The place a relocation or office question binds the user to; None when the question is no commitment."""
    low = norm(label)
    if _AUTH.search(low):
        return None
    named = places_in(label)
    start_in = bool(re.search(r"\bstart(?:\s+\w+)?\s+in\s+(?:the\s+|our\s+)?[A-Z]", label)) and bool(named)
    if not _COMMITMENT.search(low) and not start_in:
        return None
    if named:
        return named[0]
    return "" if job_remote else re.split(r"[,;/|(]", job_location)[0].strip()


def _canon(text: str) -> str:
    return re.sub(
        r" sf ", " san francisco ", re.sub(r" (nyc|new york city) ", " new york ", f" {norm(text)} ")
    )


def names_place(text: str, place: str) -> bool:
    """Whether ``text`` names ``place`` (whole words; NYC = New York City)."""
    return bool(text) and _canon(place) in _canon(text)


def _fit(form_field: FormField, raw: str | bool | None, *, day_first: bool = False) -> str | None:  # noqa: PLR0911  (one shape per field kind)
    """A raw answer shaped for the field: mapped onto one of its options when it has options."""
    if raw is None or raw == "":
        return None
    if form_field.type == "date":
        return to_date(raw, day_first=day_first) if isinstance(raw, str) else None
    options = form_field.options
    if isinstance(raw, bool):
        return yes_no_option(options, raw) if options else ("Yes" if raw else "No")
    if not options:
        return raw
    if form_field.type == "checkbox" and len(options) > 1:
        picks = pick_options(options, raw)
        return ", ".join(picks) if picks else None
    if form_field.type == "checkbox":
        return "Yes" if re.fullmatch(r"yes|y|true|1|checked|agree|i agree", raw.strip(), re.I) else None
    return best_option(options, raw)


def _from_profile(form_field: FormField, ctx: FormContext) -> str | bool | None:  # noqa: PLR0911, PLR0912  (a lookup table)
    a = ctx.applicant
    label = norm(form_field.label)
    if re.search(r"\bcover letter\b", label) and form_field.type != "file":
        return ctx.cover_letter or None
    if re.search(r"\b(first|given) name\b|^preferred (first )?name\b", label):
        return a.first_name
    if re.search(r"\b(last|family) name\b|\bsurname\b", label):
        return a.last_name
    if re.search(r"^(full |legal |your )?name$|\bfull name\b|\blegal name\b", label):
        return f"{a.first_name} {a.last_name}".strip()
    if re.search(r"\be ?mail\b", label):
        return a.email
    if re.search(r"\b(phone|mobile|cell)\b", label) and "country" not in label:
        return a.phone
    if "linkedin" in label:
        return a.linkedin
    if "github" in label:
        return a.github
    prose = form_field.type in ("text", "textarea") and len(form_field.label.split()) > 6  # noqa: PLR2004
    if not prose and re.search(r"\b(website|portfolio|personal site|blog)\b", label):
        return a.website
    residence = _RESIDENCE.search(label) is not None  # about where the user lives, not the job's country
    # "...to work in the United States?" asks about that country, whatever the job's own country is.
    named = _named_countries(form_field.label)
    authorized = bool(named & a.authorized_countries) if named and a.authorized_countries else None
    if re.search(r"\bsponsor|\bvisa\b", label):
        if residence:
            return False
        return (not authorized) if authorized is not None else a.needs_sponsorship
    if re.search(
        r"authori[sz]ed|legally (able|eligible|permitted)|right to work|eligible to work|work authori", label
    ):
        if residence:
            return True
        return authorized if authorized is not None else a.work_authorized
    if "relocat" in label:
        return a.willing_to_relocate
    if re.search(r"\b(phone|mobile|cell)\b.*\bcountry\b|\b(dial(l?ing)?|calling) code\b", label):
        return _dial_answer(form_field.options, a)
    if re.search(
        r"^country( code)?$|\bcountry\b.*\b(reside|based|located|live)\b|\b(reside|based|located|live)\b.*\bcountry\b",
        label,
    ):
        return _country_option(form_field.options, a.home_country) or a.country
    if re.search(
        r"where (are |do )?you (currently )?(located|based|live)|currently located|(what|which) city|city and state",
        label,
    ):
        return a.location
    if not prose and re.search(r"\blocation\b|\bcity\b|\baddress\b", label):
        return a.location
    if not prose and re.search(
        r"\b(current (company|employer)|company name|employer)\b|^company$|^org$", label
    ):
        return a.current_company
    if not prose and re.search(r"\b(current (job )?title|job title|current role|current position)\b", label):
        return a.current_title
    if re.search(
        r"(salary|compensation|pay|ctc) (expectation|requirement|range)|(expected|desired) (salary|compensation|pay|ctc)",
        label,
    ):
        return a.salary_expectation

    def listed(
        value: str,
    ) -> str | None:  # from a list, only the exact wording: a near miss is another degree
        return exact_option(form_field.options, value) if value and form_field.options else value

    if label == "school":
        return a.school
    if label == "degree":
        return listed(a.degree)
    if label == "discipline":
        return listed(a.discipline)
    if label == "start date year":
        return a.start_year
    if label == "end date year":
        return a.end_year
    if re.search(r"notice period|when can you start|earliest .*start|start date|available to start", label):
        return a.notice_period
    return None


def _named_countries(label: str) -> frozenset[str]:
    """Countries a question names ("authorized to work in the UK?" -> {GB}); empty when it names none."""
    codes: set[str] = set()
    for place in places_in(label):
        codes |= location_codes(place)
    return frozenset(codes)


def _country_option(options: tuple[str, ...], code: str) -> str | None:
    """The option naming country ``code`` however it is written ("UK", "United Kingdom (+44)", "GB")."""
    if not code:
        return None
    for option in options:
        if option.strip().upper() == code or location_codes(option) == {code}:
            return option
    return None


def _dial_answer(options: tuple[str, ...], a: Applicant) -> str | None:
    """A phone country-code field: the option for the user's country, else the "+44" their number starts with."""
    dial = re.match(r"\s*(\+\d{1,3})(?=[\s().-])", a.phone)
    if options:
        if dial and (hit := next((o for o in options if re.search(rf"\{dial[1]}(?!\d)", o)), None)):
            return hit
        return _country_option(options, a.home_country)
    return dial[1] if dial else None


_RESIDENCE = re.compile(
    r"country of residence|country (where |that )?you (currently )?(live|reside)|your (current|home) country|"
    r"where you (currently )?(live|reside)"
)
# "Are you a human or an AI / automated program?": only the user answers this, never Sarthi on its own.
_BOT_CHECK = re.compile(
    r"\b(ai|bot|robot|automated|automation|human being|a human)\b.*\b(ai|bot|robot|automated|program|human)\b"
)
_EEO = re.compile(
    r"hispanic|latin[oax]|transgender|sexual orientation|lgbt|\bgender\b|\bsex\b|\brac(e|ial)\b|ethnic|veteran|disabilit"
)
_HEAR = re.compile(
    r"how (did |do )?you (hear|heard|learn|learned|find|found|discover)|where did you (hear|find|learn|see)|referral source|^source$"
)
_HEAR_PREFS = (
    "company website",
    "careers page",
    "company careers",
    "website",
    "job board",
    "online",
    "internet",
    "other",
)


def _decline(options: tuple[str, ...]) -> str | None:
    return next((o for o in options if _DECLINE.search(norm(o))), None)


def suggest(form_field: FormField, ctx: FormContext) -> Suggestion | None:  # noqa: PLR0911, PLR0912  (ordered sources)
    """The value for one field, or None when only the user can answer it."""
    label = norm(form_field.label)
    if form_field.type == "file":
        if "cover" in label:
            return Suggestion(ctx.cover_file, "file") if ctx.cover_file else None
        if re.search(r"resume|\bcv\b", label):
            return Suggestion(ctx.resume_file, "file") if ctx.resume_file else None
        return None
    home = ctx.applicant.home_country
    day_first = bool(home) and home not in MONTH_FIRST_COUNTRIES  # how the user writes dates
    own = ctx.answers.get(form_field.key)
    if own is None and (hit := lookup(ctx.answers, form_field.label)):
        own = hit[1]
    if value := _fit(form_field, own, day_first=day_first):
        return Suggestion(value, "job")
    if company_specific(form_field.label):
        return None  # about this employer: only an answer given for this job (above) is used
    if _BOT_CHECK.search(f"{label} {' '.join(norm(o) for o in form_field.options)}"):
        saved = lookup(ctx.applicant.saved_answers, form_field.label)  # the user's own earlier answer only
        value = _fit(form_field, saved[1], day_first=day_first) if saved else None
        return Suggestion(value, "saved") if value else None
    if _EEO.search(label) and "pronoun" not in label:
        declined = _decline(form_field.options)
        return Suggestion(declined, "default") if declined else None
    a = ctx.applicant
    experience = experience_answer(form_field.label, form_field.options, a.years_experience, a.skills)
    if (value := _fit(form_field, experience, day_first=day_first)) is not None:
        return Suggestion(value, "profile")
    place = commitment_place(form_field.label, ctx.job_location, ctx.job_remote)
    if (not place or names_place(ctx.applicant.location, place)) and (
        value := _fit(form_field, _from_profile(form_field, ctx), day_first=day_first)
    ):
        return Suggestion(value, "profile")
    hit = lookup(ctx.applicant.saved_answers, form_field.label)
    if (
        hit
        and (not place or names_place(" ".join(hit), place))
        and (value := _fit(form_field, hit[1], day_first=day_first))
    ):
        return Suggestion(value, "saved")
    if _HEAR.search(label):
        if not form_field.options:
            return Suggestion("Company website", "default")
        for preference in _HEAR_PREFS:
            option = next((o for o in form_field.options if preference in norm(o)), None)
            if option:
                return Suggestion(option, "default")
        return None
    if "pronoun" in label and (declined := _decline(form_field.options)):
        return Suggestion(declined, "default")
    return None


def unanswered(fields: list[FormField], ctx: FormContext) -> list[FormField]:
    """Required questions (not uploads) that only the user can answer."""
    return [f for f in fields if f.required and f.type != "file" and suggest(f, ctx) is None]
