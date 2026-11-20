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


# Verbs include common typos seen in real answers ("expect Fintech" meant "except Fintech").
_REMOVE_VERB = (
    r"(?:remove|remov|rmove|without|except|expect|excpet|exept|exclude|drop|delete|minus|skip|not|no)"
)
_ADD_VERB = r"(?:add|plus|include|also\s+add|and\s+also)"
_REMOVE = re.compile(rf"^{_REMOVE_VERB}\s+(?P<item>.+)$", re.IGNORECASE)
_ADD = re.compile(rf"^{_ADD_VERB}\s+(?P<item>.+)$", re.IGNORECASE)
_KEEP = re.compile(
    r"^(?:ok(?:ay)?|yes|y|yep|sure|fine|good|correct|perfect|keep|same|all\s+good|looks\s+good|as\s+suggested|"
    r"(?:the\s+)?(?:other|others|rest|remaining)(?:\s+(?:are|is))?\s*(?:ok(?:ay)?|fine|good)?|"
    r"keep\s+(?:the\s+)?(?:other|others|rest|remaining|these|them|all)|"
    r"use\s+(?:the\s+)?(?:these|them|those|it|all|same|suggested|suggestions?|skills|roles)"
    r"(?:\s+(?:ones?|skills|roles|suggestions?))?)$",
    re.IGNORECASE,
)
_KEEP_PREFIX = re.compile(r"^(?:ok(?:ay)?|yes|sure|fine|good)[\s:-]+(?P<rest>\S.*)$", re.IGNORECASE)
_FILLER_START = re.compile(r"^(?:the|a|an|also|and|please|pls)\s+", re.IGNORECASE)
_FILLER_END = re.compile(
    r"\s+(?:also|too|as\s+well|please|pls|ones?|roles?|jobs?|positions?)$", re.IGNORECASE
)
_PURE_FILLER = frozenset({"please", "pls", "thanks", "thank you", "thx", "also", "too"})
_EVERYTHING = frozenset({"all", "everything", "all of them", "them all"})
_INSTRUCTION_WORDS = re.compile(
    rf"\b(?:{_REMOVE_VERB[3:-1]}|okay|ok|keep|rest|others?|use|suggested)\b", re.IGNORECASE
)
UNCLEAR_LIST = "I couldn't tell which items you meant. Please list them, e.g. Backend Engineer, AI Engineer."
_WORD = re.compile(r"[a-z0-9+#.]+")


@dataclass(frozen=True, slots=True)
class ListEdit:
    """The list the user wants, or a message asking again."""

    items: tuple[str, ...]
    error: str | None = None
    removed: tuple[str, ...] = ()  # suggested items the answer explicitly removed ("except fintech")


def _clean(item: str) -> str:
    """Drop filler around an item: "the microservices one" -> "microservices", "backend AI engineer also"."""
    previous = None
    while previous != item:
        previous = item
        item = _FILLER_END.sub("", _FILLER_START.sub("", item.strip())).strip()
    return item


def _matches(needle: str, item: str) -> bool:
    """True when every word of ``needle`` is in ``item`` ("fintech" matches "Fintech App Developer")."""
    words = set(_WORD.findall(needle.lower()))
    return bool(words) and words <= set(_WORD.findall(item.lower()))


def edit_list(text: str, suggestion: tuple[str, ...] | list[str]) -> ListEdit:
    """Apply an answer to a suggested list.

    Understands a fresh list ("Backend Engineer, SRE"), "ok" / "use these", and edits such as
    "remove frontend developer intern, other okay", "ok, add Data Engineer, without AI Engineer" or
    "add backend AI engineer also, except Fintech, microservices one". Items after a verb take that verb
    until another verb. Removal matches by words, ignoring case. Instruction words are never returned as
    items.
    """
    base = list(suggestion)
    keep_base = False
    removals: list[str] = []
    additions: list[str] = []
    plain: list[str] = []
    mode = "plain"
    for raw in split_list(text):
        part = raw.strip()
        if prefix := _KEEP_PREFIX.match(part):  # "ok add Data Engineer" without a comma
            keep_base, part = True, prefix["rest"]
        if _KEEP.match(part):
            keep_base, mode = True, "plain"
        elif removal := _REMOVE.match(part):
            removals.append(_clean(removal["item"]))
            mode = "remove"
        elif addition := _ADD.match(part):
            additions.append(_clean(addition["item"]))
            mode = "add"
        elif mode == "remove":
            removals.append(_clean(part))
        elif mode == "add":
            additions.append(_clean(part))
        else:
            plain.append(_clean(part))
    if any(r.lower() in _EVERYTHING for r in removals):
        base, removals = [], [r for r in removals if r.lower() not in _EVERYTHING]
    edited = keep_base or removals or additions or len(base) != len(suggestion)
    items = (base + additions + plain) if edited else plain
    items = [
        i for i in items if i and i.lower() not in _PURE_FILLER and not any(_matches(r, i) for r in removals)
    ]
    unique: dict[str, str] = {}
    for item in items:
        unique.setdefault(item.lower(), item)
    result = tuple(unique.values())[:MAX_ITEMS]
    explicitly_removed = tuple(i for i in suggestion if any(_matches(r, i) for r in removals))
    if any(_INSTRUCTION_WORDS.search(item) for item in result):
        return ListEdit((), UNCLEAR_LIST, explicitly_removed)
    return ListEdit(result, None, explicitly_removed)


_EDIT_WORDS = re.compile(
    r"\b(?:remove|remov|without|except|expect|exclude|drop|delete|add|include|instead|replace|swap|change|"
    r"only|but|not|no|keep|also|rather|first|last|all|everything|rest|others?|one|ones)\b",
    re.IGNORECASE,
)


def is_simple_answer(text: str) -> bool:
    """True for "ok"-style answers and plain lists, which the fast rules handle; edits go to the AI."""
    text = text.strip()
    return bool(_KEEP.match(text)) or not _EDIT_WORDS.search(text)


def looks_like_instruction(item: str) -> bool:
    """True when an item contains instruction words ("remove", "ok", ...), i.e. it was misread."""
    return bool(_INSTRUCTION_WORDS.search(item))


_GENERIC_WORDS = frozenset(
    {"developer", "engineer", "engineering", "application", "applications", "senior", "junior", "lead",
     "the", "and", "role", "roles", "job", "manager", "specialist", "associate"}
)  # fmt: skip
_MIN_DISTINCTIVE = 3
_RESTRICT = re.compile(r"\b(?:only|just|new\s+list|instead\s+of\s+(?:all|these|everything))\b", re.IGNORECASE)


def _mentioned(item: str, text: str) -> bool:
    """True when a distinctive word of ``item`` is in ``text`` ("fintech" for "Fintech App Developer")."""
    said = set(_WORD.findall(text.lower()))
    words = (w.removesuffix(".js") for w in _WORD.findall(item.lower()))
    distinctive = [w for w in words if len(w) >= _MIN_DISTINCTIVE and w not in _GENERIC_WORDS]
    return item.lower() in text.lower() or any(w in said or f"{w}.js" in said for w in distinctive)


def reconcile(current: list[str], ai_items: list[str], text: str) -> list[str]:
    """Check an AI-edited list against the answer, using rules that are reliable where small models are not.

    - Items the AI dropped that the user never mentioned come back (unless they said "only" or "just").
    - Items the rules clearly see removed ("except fintech") are removed even if the AI kept them.
    """
    result = list(ai_items)
    kept = {item.lower() for item in result}
    if not _RESTRICT.search(text):
        for position, item in enumerate(current):
            if item.lower() not in kept and not _mentioned(item, text):
                result.insert(min(position, len(result)), item)
                kept.add(item.lower())
    removed = {item.lower() for item in edit_list(text, current).removed}
    return [item for item in result if item.lower() not in removed]


_CURRENCY_CODES = "INR|USD|GBP|EUR|AED|CAD|AUD|SGD|JPY|CHF|SEK|NOK|DKK|PLN|NZD|HKD|SAR|QAR"
_SALARY_IN_TEXT = re.compile(
    rf"(?:(?<![a-z])(?:{_CURRENCY_CODES})\s*|[₹$£€]\s*)?\d+(?:[.,]\d+)*\s*"
    rf"(?:lakhs|lakh|lacs|lac|lpa|k|l)?\b\s*(?:(?:{_CURRENCY_CODES})\b|[₹$£€])?\s*(?:/?\s*(?:year|yr|annum|pa|month|mo)\b)?",
    re.IGNORECASE,
)


def find_salary(text: str, home_country: str) -> Salary | None:
    """A salary anywhere in a sentence ("make my minimum 15 LPA"), or None."""
    for match in _SALARY_IN_TEXT.finditer(text.replace("\u00a0", " ")):
        if salary := parse_salary(match.group().strip(), home_country):
            return salary
    return None


_REMOTE_ONLY = re.compile(
    r"\b(?:remote\s+only|only\s+remote|fully\s+remote|100%\s+remote|wfh\s+only)\b", re.I
)
_NO_REMOTE = re.compile(
    r"\b(?:no\s+remote|not\s+remote|on-?site|in\s+office|office\s+only|onsite\s+only|hybrid\s+only|"
    r"on-?site\s+or\s+hybrid|office\s+or\s+hybrid|hybrid\s+or\s+office|from\s+(?:the\s+)?office)\b",
    re.IGNORECASE,
)
_ANY_REMOTE = re.compile(
    r"\b(?:any|anything|all|both|remote\s+(?:is\s+)?(?:ok|fine)|remote\s+or|or\s+remote|hybrid)\b", re.I
)


def parse_remote_text(text: str) -> RemotePreference | None:
    """Remote preference from a sentence ("actually remote only", "office or hybrid please")."""
    if exact := parse_remote(text):
        return exact
    if _REMOTE_ONLY.search(text):
        return RemotePreference.ONLY
    if _NO_REMOTE.search(text):
        return RemotePreference.NO
    if _ANY_REMOTE.search(text):
        return RemotePreference.OK
    return None
