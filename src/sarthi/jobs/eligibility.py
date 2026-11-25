"""Who a job is open to, read from its own text without AI: citizenship, residency, clearance and languages.

Ideas (not code) from ai-job-search's job-evaluation guide (MIT): a job that names a citizenship, permanent
residency or security clearance, or a language as a condition, is not for someone who lacks it. Only what the
posting states counts: silence is no requirement. The language an ad is written in says nothing on its own
(an English-speaking team may advertise in German); only a language asked of the applicant does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---- citizenship, permanent residency, security clearance -------------------------------------------------

# Nationality words and the country they stand for ("Australian citizenship", "US citizens only").
_NATIONALITY: dict[str, str] = {
    "us": "US", "u.s.": "US", "usa": "US", "american": "US", "united states": "US",
    "uk": "GB", "british": "GB", "united kingdom": "GB",
    "australian": "AU", "australia": "AU", "canadian": "CA", "canada": "CA",
    "german": "DE", "germany": "DE", "dutch": "NL", "netherlands": "NL",
    "singaporean": "SG", "singapore": "SG", "emirati": "AE", "uae": "AE",
    "indian": "IN", "india": "IN", "irish": "IE", "ireland": "IE", "new zealand": "NZ",
    "french": "FR", "israeli": "IL", "saudi": "SA", "japanese": "JP",
}  # fmt: skip
_NATION = "|".join(re.escape(n) for n in sorted(_NATIONALITY, key=len, reverse=True))
_REQUIRED = r"(?:required|only|mandatory|is a must|needed|necessary|essential)"
# Each pattern's "nation" group (when present) names the country.
_CITIZENSHIP = [
    re.compile(rf"\b(?P<nation>{_NATION})\s+citizen(?:s|ship)?\s+(?:is\s+)?{_REQUIRED}", re.I),
    re.compile(rf"\bonly\s+(?:open\s+to\s+)?(?P<nation>{_NATION})\s+citizens\b", re.I),
    re.compile(
        rf"\bmust\s+(?:be|hold)\s+(?:an?\s+)?(?:(?P<nation>{_NATION})\s+)?"
        r"(?:citizen|national|citizenship|permanent\s+resident|green\s+card)",
        re.I,
    ),
    re.compile(
        rf"\bcitizen(?:s|ship)\s+(?:of\s+(?:the\s+)?(?P<nation>{_NATION})\s+)?(?:is\s+)?{_REQUIRED}", re.I
    ),
    re.compile(
        rf"\bcitizens\s+(?:and|or)\s+(?:permanent\s+residents|green\s+card\s+holders)\s+{_REQUIRED}", re.I
    ),
    re.compile(
        rf"\b(?:(?P<nation>{_NATION})\s+)?(?:permanent\s+residen(?:t|cy|ts)|green\s+card(?:\s+holders?)?|"
        rf"PR\s+(?:status|holders?|visa))\s+(?:status\s+)?(?:is\s+)?{_REQUIRED}",
        re.I,
    ),
]
_CLEARANCE = re.compile(
    r"\b(?:security\s+clearance|(?:top\s+secret|secret|TS/SCI|baseline|NV1|NV2)\s+(?:security\s+)?clearance|"
    r"(?-i:SC|DV|TS)\s+(?:level\s+)?clear(?:ance|ed)|clearance\s+(?:level\s+)?(?-i:SC|DV)\b)",
    re.I,
)
# "Security clearance is not required", "no citizenship requirement", "regardless of citizenship".
_NOT_ASKED = re.compile(
    r"\b(?:not|no|never|without|regardless\s+of|irrespective\s+of)\b[^.;\n]{0,40}$|"
    r"^[^.;\n]{0,30}\b(?:not\s+(?:required|needed|necessary)|is\s+not\b)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Restriction:
    """A citizenship-like condition: the country it names (None: the job's own) and the posting's words."""

    country: str | None
    phrase: str


def _quote(text: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", text[start:end]).strip()[:80]


def _asked(text: str, start: int, end: int) -> bool:
    """False when the phrase is negated around it ("clearance is not required")."""
    return not (_NOT_ASKED.search(text[max(0, start - 45) : start]) or _NOT_ASKED.match(text[end : end + 40]))


def citizenship_restrictions(text: str) -> list[Restriction]:
    """Citizenship, permanent residency or security clearance the posting requires."""
    found: list[Restriction] = []
    for pattern in _CITIZENSHIP:
        for match in pattern.finditer(text):
            if not _asked(text, match.start(), match.end()):
                continue
            nation = match.groupdict().get("nation")
            country = _NATIONALITY.get(nation.lower()) if nation else None
            found.append(Restriction(country, _quote(text, match.start(), match.end())))
    for match in _CLEARANCE.finditer(text):
        if _asked(text, match.start(), match.end()):
            found.append(Restriction(None, _quote(text, match.start(), match.end())))
    return found


# ---- languages --------------------------------------------------------------------------------------------

LANGUAGES = (
    "German", "French", "Dutch", "Spanish", "Italian", "Portuguese", "Arabic", "Japanese", "Mandarin",
    "Chinese", "Cantonese", "Korean", "Polish", "Swedish", "Norwegian", "Danish", "Finnish", "Hebrew",
    "Turkish", "Russian", "Czech", "Hungarian", "Romanian", "Greek", "Malay", "Indonesian", "Thai",
    "Vietnamese", "Hindi", "Tamil", "Telugu", "Bengali", "Urdu", "Flemish", "English",
)  # fmt: skip
_NATIVE_NAMES = {"deutsch": "German", "nederlands": "Dutch", "français": "French", "francais": "French"}
_LANGUAGE = "|".join([*LANGUAGES, *(n.capitalize() for n in _NATIVE_NAMES)])
# CEFR levels as numbers; words people use for them.
_LEVELS = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}
_LEVEL_WORDS = {
    "native": 6, "mother tongue": 6, "bilingual": 6, "fluent": 5, "fluency": 5, "fließend": 5,
    "fliessend": 5, "excellent": 5, "sehr gute": 5, "business": 4, "professional": 4, "proficient": 4,
    "proficiency": 4, "strong": 4, "good": 3, "gute": 3, "working": 3, "conversational": 3,
    "intermediate": 3, "basic": 2, "beginner": 1,
}  # fmt: skip
_LEVEL_WORD = "|".join(re.escape(w) for w in sorted(_LEVEL_WORDS, key=len, reverse=True))
_CEFR = r"[ABC][12]"
# A language the applicant must have. The language's capital letter is required: "german" in a URL or
# "German shepherd" has no requirement words around it anyway.
_LANGUAGE_ASKED = [
    # "fluent German", "C1 German", "business-level French", "excellent command of Dutch", "fluency in Arabic"
    re.compile(
        rf"\b(?P<level>{_LEVEL_WORD}|{_CEFR})(?:[\s-]+(?:level|command|knowledge|skills|proficiency))?"
        rf"(?:\s+(?:of|in))?(?:\s+(?:the\s+)?(?:spoken\s+and\s+written\s+)?)\s*(?-i:(?P<lang>{_LANGUAGE}))\b",
        re.I,
    ),
    # Language then level or requirement: German (C1), German at C1 level, German is required, Arabic speaker.
    re.compile(
        rf"(?-i:\b(?P<lang>{_LANGUAGE}))(?:\s+language)?\s*(?:\(?\s*(?:at\s+)?(?P<level>{_CEFR})\b\)?|"
        rf"(?:skills\s+|proficiency\s+|knowledge\s+)?(?:is\s+|are\s+)?(?:required|mandatory|a\s+must|essential|"
        r"necessary|needed)|[\s-]speaker\b)",
        re.I,
    ),
    # "must speak German", "you speak fluent French", "ability to communicate in Dutch"
    re.compile(
        rf"\b(?:must|need\s+to|required\s+to|able\s+to|ability\s+to)\s+(?:speak|communicate\s+in|write\s+in)\s+"
        rf"(?:(?P<level>{_LEVEL_WORD})\s+)?(?-i:(?P<lang>{_LANGUAGE}))\b",
        re.I,
    ),
    # German postings: "sehr gute Deutschkenntnisse", "fließend Deutsch"
    re.compile(rf"\b(?P<level>{_LEVEL_WORD})?\s*(?P<lang>Deutsch)(?:kenntnisse|\b)", re.I),
]
# "German is a plus", "French would be nice to have": wished for, not required.
_OPTIONAL = re.compile(
    r"\b(?:plus|bonus|nice[\s-]to[\s-]have|advantage(?:ous)?|preferred|desirable|beneficial|is\s+a\s+benefit|"
    r"ideally|helpful|not\s+required|not\s+necessary|optional|wünschenswert|von\s+vorteil)\b",
    re.I,
)
# Words that make a match a condition by themselves ("fluent German", "German skills", "German (C1)"); a
# weaker match ("good German", "Arabic speaker") needs a requirement word in its sentence, so "excellent
# French cuisine" or "our Arabic speakers" asks nothing.
_STRONG = re.compile(
    r"fluen|native|mother tongue|bilingual|\b[ABC][12]\b|fließend|fliessend|kenntnisse|required|mandatory|"
    r"must|essential|necessary|needed|skills|knowledge|command|proficien|level|written|spoken",
    re.I,
)
_CUE = re.compile(
    r"\b(?:must|required|requirements?|need(?:s|ed)?|you\s+(?:are|have|speak|bring)|we\s+(?:expect|require)|"
    r"looking\s+for|seeking|essential)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class LanguageAsk:
    """A language the posting requires, the level asked (CEFR 1-6; None: not said) and its words."""

    language: str
    level: int | None
    phrase: str


def level_name(level: int) -> str:
    """The CEFR name of a level number (5 -> "C1")."""
    return next((name.upper() for name, number in _LEVELS.items() if number == level), str(level))


def _level(word: str | None) -> int | None:
    if not word:
        return None
    word = word.lower().strip()
    return _LEVELS.get(word) or _LEVEL_WORDS.get(word)


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence (or list item) around a match."""
    before = max(text.rfind(c, 0, start) for c in ".;\n•")
    after = [i for i in (text.find(c, end) for c in ".;\n•") if i != -1]
    return text[before + 1 : min(after) if after else len(text)]


def language_requirements(text: str) -> list[LanguageAsk]:
    """Languages the posting asks of the applicant as a condition (a "plus" is not a condition)."""
    asks: dict[str, LanguageAsk] = {}
    for pattern in _LANGUAGE_ASKED:
        for match in pattern.finditer(text):
            lang_word = match["lang"]
            language = _NATIVE_NAMES.get(lang_word.lower(), lang_word.capitalize())
            if language not in LANGUAGES:
                continue
            if (
                pattern is _LANGUAGE_ASKED[-1]
                and not match["level"]
                and "kenntnisse" not in match.group().lower()
            ):
                continue  # a bare "Deutsch" (the ad's own language, a heading) asks nothing
            sentence = _sentence(text, match.start(), match.end())
            if _OPTIONAL.search(sentence) or not (_STRONG.search(match.group()) or _CUE.search(sentence)):
                continue
            if not _asked(text, match.start(), match.end()):
                continue
            level = _level(match.groupdict().get("level"))
            known = asks.get(language)
            if known is None or (level or 0) > (known.level or 0):
                asks[language] = LanguageAsk(language, level, _quote(text, match.start(), match.end()))
    return list(asks.values())


def spoken_languages(listed: tuple[str, ...] | list[str]) -> dict[str, int | None]:
    """The user's languages ("German B1", "French (fluent)", "Dutch") as name -> level (None: not said).

    English is always included: Sarthi's documents and setup are in English.
    """
    spoken: dict[str, int | None] = {"English": None}
    for item in listed:
        name = re.search(rf"(?i)\b({_LANGUAGE})\b", item)
        if not name:
            continue
        language = _NATIVE_NAMES.get(name[1].lower(), name[1].capitalize())
        level = re.search(rf"(?i)\b({_CEFR}|{_LEVEL_WORD})\b", item)
        spoken[language] = _level(level[1]) if level else None
    return spoken
