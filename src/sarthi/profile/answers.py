"""Short typed answers to profile values: "India, UK" -> {"IN", "GB"}, "12 LPA" -> INR 1,200,000."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sarthi.jobs.countries import ALL as _ISO_NAMES
from sarthi.jobs.geo import first_country, location_codes, resolve_place
from sarthi.jobs.models import Salary, SalaryPeriod
from sarthi.profile.models import RemotePreference

NONE_WORDS = frozenset({"none", "no", "nothing", "skip", "-", "na", "n/a", "nil"})
MAX_ITEMS = 40  # long enough for a resume's skills plus additions; never silently drop more
MAX_YEARS = 60
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "half": "0.5", "a": "1", "an": "1",
}  # fmt: skip
_NUMBER_WORD = re.compile(
    r"\b(?:" + "|".join(_NUMBER_WORDS) + r")\b(?=\s*(?:years?|yrs?|months?|mos?))", re.I
)
_AND_A_HALF = re.compile(r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")\s+and\s+(?:a\s+)?half\b\s*", re.I)


def _number(word: str) -> float:
    return float(_NUMBER_WORDS.get(word.lower(), word))


_HALF_YEAR = re.compile(r"\bhalf\s+(?:a\s+)?year\b", re.IGNORECASE)
_FRESHER = frozenset({"fresher", "fresh", "0", "none", "new grad", "student", "no experience", "nil", "zero"})
# "/" and "&" separate items only with spaces around them: "UI/UX Designer", "R&D Engineer" stay whole.
_SPLIT = re.compile(r"\s*(?:,|;|\n|\s/\s|\band\b|\s&\s)\s*", re.IGNORECASE)
_LAKH = 100_000
_THOUSAND = 1_000
_CURRENCY_CODES = "INR|USD|GBP|EUR|AED|CAD|AUD|SGD|JPY|CHF|SEK|NOK|DKK|PLN|NZD|HKD|SAR|QAR"
_SALARY = re.compile(
    rf"^(?P<cur1>{_CURRENCY_CODES}|[₹$£€]|rs\.?)?\s*(?P<amount>\d+(?:[.,]\d+)*)\s*"
    r"(?P<unit>crores?|cr|lakhs?|lacs?|lpa|k|l)?\b\.?\s*"
    rf"(?:(?P<cur2>{_CURRENCY_CODES})\b|(?P<sym2>[₹$£€]))?\s*"
    r"(?:/|per|a|an|p\.?)?\s*(?P<period>years?|yr|annum|pa|month|months|mo|monthly|yearly|annually|m)?\.?$",
    re.IGNORECASE,
)
_SKIP_SALARY = re.compile(
    r"^(?:skip|none|no|nil|na|n/a|-|negotiable|not\s+sure|don'?t\s+know|no\s+idea|any|anything|"
    r"no\s+minimum|doesn'?t\s+matter|flexible|open)[\s.!]*$",
    re.IGNORECASE,
)
_CRORE = 10_000_000
_SMALL_INR_IS_LAKHS = 1_000  # "12" from someone in India means 12 lakh a year
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
        cleaned = item.strip(" -").rstrip(".").strip()  # keep a leading dot: ".NET"
        if cleaned and cleaned.lower() not in seen:
            seen[cleaned.lower()] = cleaned[:120]
    return tuple(seen.values())[:MAX_ITEMS]


@dataclass(frozen=True, slots=True)
class Countries:
    """Recognised ISO codes, and the items that could not be placed."""

    codes: frozenset[str]
    unknown: tuple[str, ...]
    anywhere: bool = False
    nothing: bool = False  # the user said "none" / "only India": an answer, not a blank


ALL_COUNTRIES: frozenset[str] = frozenset(_ISO_NAMES)
_ANYWHERE = re.compile(
    r"\b(?:anywhere|any\s*where|worldwide|world\s*wide|globally|global|all\s+(?:the\s+)?countries|"
    r"any\s+country|every\s+country|everywhere|open\s+to\s+all|whole\s+world)\b",
    re.IGNORECASE,
)
_HOME_ONLY = re.compile(
    r"^(?:(?:only|just)\s+(?:in\s+)?(?:my\s+)?(?:home(?:\s+country)?|here|india|my\s+country)|"
    r"(?:my\s+)?home(?:\s+country)?\s+only|no(?:ne)?\s+(?:other|else|more)|nowhere(?:\s+else)?|"
    r"same(?:\s+country)?|not\s+(?:abroad|outside))[\s.!]*$",
    re.IGNORECASE,
)
_ABBREVIATIONS = {
    "UK": "UK", "U.K.": "UK", "USA": "USA", "US": "US", "U.S.": "US", "U.S.A.": "USA",
    "UAE": "UAE", "KSA": "KSA", "EU": "EU",
}  # fmt: skip
_FILLER_WORDS = re.compile(
    r"\b(?:i'?d|i\s+would|i\s+can|i\s+could|i\s+have|i\s+hold|have|has|hold|also|maybe|and|or|work|working|"
    r"in|to|the|countries|country|like|prefer|preferably|ok|okay|fine|with|please|would|be|open|happy|go|move|"
    r"relocate|an?|my|passport|citizen|citizenship|green\s+card|permit|work\s+permit|pr|visa|holder|resident)\b",
    re.IGNORECASE,
)
_LETTERS = re.compile(r"[a-z]", re.IGNORECASE)


def parse_countries(text: str) -> Countries:
    """Country and region names to ISO codes.

    "Europe" expands to its countries; "anywhere" / "worldwide" / "all countries" gives every country
    (``Countries.anywhere``); "none" / "only India" / "just home" gives none. Names inside a phrase are found
    ("I'd also work in UK and UAE"); only items with no recognisable place are reported as unknown.
    """
    if is_none(text) or _HOME_ONLY.match(text.strip()):
        return Countries(frozenset(), (), nothing=True)
    if _ANYWHERE.search(text):
        return Countries(ALL_COUNTRIES, (), anywhere=True)
    codes: set[str] = set()
    unknown: list[str] = []
    for item in split_list(text):
        if re.fullmatch(r"[A-Z]{2}", item.strip()) and item.strip() in ALL_COUNTRIES:
            codes.add(item.strip())  # an ISO code typed as such: "IN", "GB" (before "in" is read as filler)
            continue
        bare = _FILLER_WORDS.sub(" ", item).strip()
        if bare.upper() in _ABBREVIATIONS:
            bare = _ABBREVIATIONS[bare.upper()]
        resolved = resolve_place(item) or resolve_place(bare)
        if resolved and "*" not in resolved:
            codes |= resolved
            continue
        found = location_codes(item) or ({c} if (c := first_country(item)) else set())
        if found:
            codes |= found
        elif _LETTERS.search(_FILLER_WORDS.sub(" ", item)):
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
    """Years of experience: "4", "3.5 years", "two years", "1 year 6 months", "6 months", "fresher" (0).

    Ranges take the lower end ("2-3" -> 2). Returns None for anything unclear, negative or over 60 years.
    """
    answer = _HALF_YEAR.sub("0.5 years", text.strip().lower())
    answer = _AND_A_HALF.sub(lambda m: f"{_number(m.group(1)) + 0.5:g} ", answer)
    answer = _NUMBER_WORD.sub(lambda m: _NUMBER_WORDS[m.group().lower()], answer)
    if answer in _FRESHER or re.fullmatch(
        r"(?:i\s+am\s+(?:a\s+)?)?(?:fresher|fresh\s+graduate|student)", answer
    ):
        return 0.0
    if re.search(r"-\s*\d", answer) and not re.search(r"\d\s*-\s*\d", answer):
        return None  # negative
    years = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:years?|yrs?|y\b)", answer)
    months = re.search(r"(\d+)\s*(?:months?|mos?|mths?)\b", answer)
    if years is None and months is None:
        bare = re.fullmatch(r"(?:around|about|approx\.?|~)?\s*(\d+(?:\.\d+)?)\s*\+?(?:\s*-\s*\d+)?", answer)
        if bare is None:
            return None
        total = float(bare.group(1))
    else:
        total = (float(years.group(1)) if years else 0.0) + (int(months.group(1)) / 12 if months else 0.0)
    total = round(total, 1)
    return total if 0 <= total <= MAX_YEARS else None


def is_salary_skip(text: str) -> bool:
    """True for answers meaning "no minimum": skip, negotiable, not sure, any..."""
    return bool(_SKIP_SALARY.match(text.strip()))


def parse_salary(text: str, home_country: str) -> Salary | None:
    """A minimum salary: "12 LPA", "15 lakh", "1.2 crore", "80k GBP", "USD 120000", "50000 per month".

    The currency defaults to the home country's. A bare small number follows local habit ("12" in India is
    12 lakh a year, "120" in USD is 120k). Returns None when the text is not understood.
    """
    match = _SALARY.match(
        text.strip().replace("\u00a0", " ").replace("₹ ", "₹")
    )  # no-break spaces from phones
    if match is None:
        return None
    amount = float(match["amount"].replace(",", ""))
    unit = (match["unit"] or "").lower().rstrip("s")
    raw_currency = (match["cur1"] or match["cur2"] or match["sym2"] or "").lower().rstrip(".")
    if raw_currency == "rs":
        raw_currency = "inr"
    if unit in {"crore", "cr"}:
        amount *= _CRORE
    elif unit == "k":
        amount *= _THOUSAND
    elif unit:
        amount *= _LAKH
    in_inr = unit not in {"", "k"} or raw_currency in {"inr", "₹"}
    currency = _SYMBOLS.get(raw_currency, raw_currency.upper()) if raw_currency else ""
    currency = currency or ("INR" if in_inr else _HOME_CURRENCY.get(home_country, ""))
    if not unit and amount < _SMALL_INR_IS_LAKHS:
        amount *= _LAKH if currency == "INR" else _THOUSAND
    if len(currency) != 3 or amount <= 0:  # noqa: PLR2004  (ISO 4217 codes have three letters)
        return None
    monthly = (match["period"] or "").lower() in {"month", "months", "mo", "monthly", "m"}
    return Salary(
        currency=currency, period=SalaryPeriod.MONTH if monthly else SalaryPeriod.YEAR, minimum=amount
    )


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


_TAGS = re.compile(r"<[^>]*>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028-\u202e\ufeff]")  # not \t \n \r
MAX_ITEM_CHARS = 80
NOT_A_LIST_ITEM = "That doesn't look like a list. Please type names, e.g. Backend Engineer, Data Analyst."


def normalize_answer(text: str) -> str:
    """A typed answer with markup tags, invisible characters and extra spaces removed (do this first)."""
    text = _CONTROL.sub("", _TAGS.sub(" ", text))
    return re.sub(r"[ \t]+", " ", text).strip()


def clean_item(item: str) -> str | None:
    """A list item fit to save, or None when it has no letters or is too long to be a name.

    Tags and invisible characters are removed and spaces tidied.
    """
    text = _CONTROL.sub("", _TAGS.sub("", item))
    text = re.sub(r"\s+", " ", text).strip(" ,;:-*•").rstrip(".")  # keep a leading dot: ".NET"
    if not re.search(r"[^\W\d_]", text) or len(text) > MAX_ITEM_CHARS:
        return None
    return text


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
    item_words = set(_WORD.findall(item.lower()))
    item_words |= {part for word in item_words for part in word.split(".") if part}  # "node.js" -> "node"
    return bool(words) and words <= item_words


def edit_list(text: str, suggestion: tuple[str, ...] | list[str]) -> ListEdit:
    """Apply an answer to a suggested list.

    Understands a fresh list ("Backend Engineer, SRE"), "ok" / "use these", and edits such as
    "remove frontend developer intern, other okay", "ok, add Data Engineer, without AI Engineer" or
    "add backend AI engineer also, except Fintech, microservices one". Items after a verb take that verb
    until another verb. Removal matches by words, ignoring case. Instruction words are never returned as
    items.
    """
    text = normalize_answer(text)
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
    typed = [clean_item(i) for i in additions + plain if i.lower() not in _PURE_FILLER]
    if any(item is None for item in typed) and not any(typed):
        return ListEdit((), NOT_A_LIST_ITEM)
    new = [item for item in typed if item]
    if any(looks_like_instruction(item) for item in new):  # only what the user typed: suggestions are trusted
        return ListEdit((), UNCLEAR_LIST)
    items = (base + new) if edited else new
    items = [i for i in items if not any(_matches(r, i) for r in removals)]
    unique: dict[str, str] = {}
    for item in items:
        unique.setdefault(item.lower(), item)
    explicitly_removed = tuple(i for i in suggestion if any(_matches(r, i) for r in removals))
    return ListEdit(tuple(unique.values())[:MAX_ITEMS], None, explicitly_removed)


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


_SALARY_IN_TEXT = re.compile(
    rf"(?:(?<![a-z])(?:{_CURRENCY_CODES})\s*|[₹$£€]\s*)?\d+(?:[.,]\d+)*\s*"
    rf"(?:crores?|cr|lakhs?|lacs?|lpa|k|l)?\b\.?\s*(?:(?:{_CURRENCY_CODES})\b|[₹$£€])?\s*"
    r"(?:(?:/|per|a|an|p\.?)?\s*(?:years?|yr|annum|pa|months?|mo|monthly|yearly|annually|m)\b)?",
    re.IGNORECASE,
)


def find_salary(text: str, home_country: str) -> Salary | None:
    """A salary: the whole answer ("12", "50k per month"), or one inside a sentence ("make it 15 LPA").

    Inside a sentence a number needs a unit or currency ("12 apples" is not a salary). Negative amounts are
    never salaries.
    """
    text = text.replace("\u00a0", " ").strip()
    if re.search(r"(?<![\w])-\s*\d", text):
        return None
    if whole := parse_salary(text, home_country):
        return whole
    for match in _SALARY_IN_TEXT.finditer(text):
        piece = match.group().strip()
        if re.fullmatch(r"[\d.,\s]+", piece):
            continue  # a bare number inside a sentence: not clearly a salary
        if salary := parse_salary(piece, home_country):
            return salary
    return None


_REMOTE_ONLY = re.compile(
    r"^\s*(?:remote|wfh|work\s+from\s+home|fully\s+remote)[\s.!]*$|"
    r"\b(?:remote\s+only|only\s+remote|fully\s+remote|100%\s+remote|wfh\s+only|only\s+wfh)\b",
    re.IGNORECASE,
)
_NO_REMOTE = re.compile(
    r"^\s*(?:hybrid|office|on-?site|in-?office)[\s.!]*$|"
    r"\b(?:no\s+remote|not\s+remote|on-?site|in\s+office|office\s+only|onsite\s+only|hybrid\s+only|"
    r"on-?site\s+or\s+hybrid|office\s+or\s+hybrid|hybrid\s+or\s+office|from\s+(?:the\s+)?office)\b",
    re.IGNORECASE,
)
_ANY_REMOTE = re.compile(
    r"\b(?:any|anything|all|both|either|whatever|don'?t\s+care|doesn'?t\s+matter|no\s+preference|"
    r"remote\s+(?:is\s+)?(?:ok|fine)|remote\s+or|or\s+remote|all\s+(?:are\s+)?(?:ok|fine))\b",
    re.IGNORECASE,
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
