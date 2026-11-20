"""Short typed answers to profile values: "India, UK" -> {"IN", "GB"}, "12 LPA" -> INR 1,200,000."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sarthi.jobs.geo import resolve_place
from sarthi.jobs.models import Salary, SalaryPeriod
from sarthi.profile.models import RemotePreference

NONE_WORDS = frozenset({"none", "no", "nothing", "skip", "-", "na", "n/a", "nil"})
MAX_ITEMS = 20
MAX_YEARS = 60
_SPLIT = re.compile(r"\s*(?:,|;|\n|/|\band\b|&)\s*", re.IGNORECASE)
_LAKH = 100_000
_THOUSAND = 1_000
_SALARY = re.compile(
    r"^(?P<cur1>[a-z]{3}|[₹$£€])?\s*(?P<amount>\d+(?:[.,]\d+)*)\s*(?P<unit>lakhs|lakh|lacs|lac|lpa|k|l)?\b\s*"
    r"(?P<cur2>[a-z]{3}|[₹$£€])?\s*(?:/?\s*(?P<period>year|yr|annum|pa|month|mo))?$",
    re.IGNORECASE,
)
_SYMBOLS = {"₹": "INR", "$": "USD", "£": "GBP", "€": "EUR"}
_HOME_CURRENCY = {
    "IN": "INR", "US": "USD", "GB": "GBP", "CA": "CAD", "AU": "AUD", "SG": "SGD", "AE": "AED",
    "DE": "EUR", "FR": "EUR", "NL": "EUR", "IE": "EUR", "ES": "EUR", "IT": "EUR", "JP": "JPY",
}  # fmt: skip


def is_none(text: str) -> bool:
    """True for answers meaning "none" or "skip"."""
    return text.strip().lower() in NONE_WORDS


def split_list(text: str) -> tuple[str, ...]:
    """Comma, slash, "and" or line separated items, cleaned and de-duplicated (case-insensitively)."""
    if is_none(text):
        return ()
    seen: dict[str, str] = {}
    for item in _SPLIT.split(text):
        cleaned = item.strip(" .-").strip()
        if cleaned and cleaned.lower() not in seen:
            seen[cleaned.lower()] = cleaned[:120]
    return tuple(seen.values())[:MAX_ITEMS]


@dataclass(frozen=True, slots=True)
class Countries:
    """Recognised ISO codes, and the items that could not be placed."""

    codes: frozenset[str]
    unknown: tuple[str, ...]


def parse_countries(text: str) -> Countries:
    """Country and region names to ISO codes ("Europe" expands; "anywhere" is ignored)."""
    codes: set[str] = set()
    unknown: list[str] = []
    for item in split_list(text):
        resolved = resolve_place(item)
        if resolved and "*" not in resolved:
            codes |= resolved
        elif resolved is None:
            unknown.append(item)
    return Countries(frozenset(codes), tuple(unknown))


def parse_remote(text: str) -> RemotePreference | None:
    """``ok`` / ``only`` / ``no`` from a button or typed answer."""
    answer = text.strip().lower()
    for preference in RemotePreference:
        if answer == preference.value:
            return preference
    return None


def parse_years(text: str) -> float | None:
    """Years of experience from "4", "3.5 years" or "fresher" (0)."""
    answer = text.strip().lower()
    if answer in {"fresher", "fresh", "0", "none", "new grad", "student"}:
        return 0.0
    match = re.search(r"\d+(?:\.\d+)?", answer)
    if match is None:
        return None
    years = float(match.group())
    return years if 0 <= years <= MAX_YEARS else None


def parse_salary(text: str, home_country: str) -> Salary | None:
    """A yearly (or monthly) minimum: "12 LPA", "15 lakh", "80k GBP", "USD 120000", "50000 / month".

    The currency defaults to the home country's. Returns None when the text is not understood.
    """
    match = _SALARY.match(text.strip().replace("\u00a0", " "))  # no-break spaces from phones
    if match is None:
        return None
    amount = float(match["amount"].replace(",", ""))
    unit = (match["unit"] or "").lower()
    if unit == "k":
        amount *= _THOUSAND
    elif unit:
        amount *= _LAKH
    raw_currency = match["cur1"] or match["cur2"] or ("INR" if unit and unit != "k" else None)
    currency = _SYMBOLS.get(
        raw_currency or "", (raw_currency or _HOME_CURRENCY.get(home_country, "")).upper()
    )
    if len(currency) != 3 or amount <= 0:  # noqa: PLR2004  (ISO 4217 codes have three letters)
        return None
    period = SalaryPeriod.MONTH if (match["period"] or "").lower() in {"month", "mo"} else SalaryPeriod.YEAR
    return Salary(currency=currency, period=period, minimum=amount)


_REMOVE = re.compile(
    r"^(?:remove|without|except|exclude|drop|delete|minus|not|no)\s+(?P<item>.+)$", re.IGNORECASE
)
_ADD = re.compile(r"^(?:add|plus|also|and also)\s+(?P<item>.+)$", re.IGNORECASE)
_KEEP = re.compile(
    r"^(?:ok(?:ay)?|yes|y|fine|keep|same|"
    r"(?:the\s+)?(?:other|others|rest|remaining)(?:\s+(?:are|is))?\s*(?:ok(?:ay)?|fine|good)?|"
    r"keep\s+(?:the\s+)?(?:other|others|rest|remaining))$",
    re.IGNORECASE,
)
_EVERYTHING = frozenset({"all", "everything", "all of them", "them all"})
_INSTRUCTION_WORDS = re.compile(r"\b(?:remove|without|except|okay|ok|keep|rest|others?)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ListEdit:
    """The list the user wants, or a message asking again."""

    items: tuple[str, ...]
    error: str | None = None


def edit_list(text: str, suggestion: tuple[str, ...] | list[str]) -> ListEdit:
    """Apply an answer to a suggested list.

    Understands a fresh list ("Backend Engineer, SRE"), "ok", and edits of the suggestion such as
    "remove frontend developer intern, other okay" or "ok, add Data Engineer, without AI Engineer".
    Removal matches by name containment, ignoring case. Never returns instruction words as items.
    """
    base = list(suggestion)
    keep_base = False
    removals: list[str] = []
    additions: list[str] = []
    plain: list[str] = []
    mode = "plain"  # "remove trainee and intern": items after "remove" are removals until another verb
    for part in split_list(text):
        if _KEEP.match(part):
            keep_base, mode = True, "plain"
        elif removal := _REMOVE.match(part):
            removals.append(removal["item"].strip())
            mode = "remove"
        elif addition := _ADD.match(part):
            additions.append(addition["item"].strip())
            mode = "add"
        elif mode == "remove":
            removals.append(part)
        elif mode == "add":
            additions.append(part)
        else:
            plain.append(part)
    if any(r.lower() in _EVERYTHING for r in removals):
        base, removals = [], [r for r in removals if r.lower() not in _EVERYTHING]
    edited = keep_base or removals or additions or len(base) != len(suggestion)
    items = (base + additions + plain) if edited else plain
    for removal_text in removals:
        needle = removal_text.lower()
        items = [i for i in items if needle not in i.lower() and i.lower() not in needle]
    unique: dict[str, str] = {}
    for item in items:
        unique.setdefault(item.lower(), item)
    result = tuple(unique.values())[:MAX_ITEMS]
    if any(_INSTRUCTION_WORDS.search(item) for item in result):
        return ListEdit(
            (), "I couldn't tell which items you meant. Please list them, e.g. Backend Engineer, AI Engineer."
        )
    return ListEdit(result)
