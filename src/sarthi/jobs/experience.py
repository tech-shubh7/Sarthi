"""Experience questions on application forms, answered from the profile without AI.

LinkedIn Easy Apply and many company forms ask "How many years of work experience do you have with Python?",
"How many years of engineering experience do you have?" or "Do you have at least 3 years of experience with
React?". The honest answers are in the profile: the total years of work experience, and the skills on the
resume. A skill the resume does not list is never claimed: that question still goes to the user.

The subject of such a question ("python", "english") also guards the answer bank: an answer given for Python
must never be reused for Java just because the two questions share every other word.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable


def norm(text: str) -> str:
    """Lower case, apostrophes dropped, everything but letters and digits made a single space."""
    return re.sub(r"[\W_]+", " ", re.sub(r"['’]", "", text.lower())).strip()  # noqa: RUF001


# Skill names whose punctuation is the name: C++ and C# are not C, .NET is not NET.
_SYMBOL_SKILLS = ((re.compile(r"c\+\+"), " cplusplus "), (re.compile(r"c#"), " csharp "),
                  (re.compile(r"f#"), " fsharp "), (re.compile(r"(?<![\w])\.net\b"), " dotnet "))  # fmt: skip


def _canon(text: str) -> str:
    low = text.lower()
    for pattern, word in _SYMBOL_SKILLS:
        low = pattern.sub(word, low)
    return norm(low)


# "experience" questions about work in general, not one skill: answered with the total years
_GENERAL = frozenset(
    {
        "", "work", "working", "professional", "total", "overall", "relevant", "industry", "full time",
        "fulltime", "paid", "job", "career", "engineering", "software", "software engineering",
        "software development", "development", "it", "tech", "technology", "technical", "programming",
        "coding", "hands on", "practical", "corporate", "post graduation", "post qualification",
    }
)  # fmt: skip
_NOISE = re.compile(
    r"\b(programming|scripting|query)? ?languages?\b|\bframeworks?\b|\blibrary\b|\btools?\b|\bplatforms?\b|"
    r"\btechnologies\b|\btechnology\b|\bdevelopment\b|\bdevelop(ing|er)?\b|\bthe\b|\ba\b|\ban\b"
)
_SUBJECT_PATTERNS = (
    # "... years of work experience do you have with Python", "experience in AWS", "experience using React"
    re.compile(
        r"\bexperience\b(?: do you (?:currently )?have)?(?: working)? (?:with|in|using|on|of) (?P<s>.+)$"
    ),
    # "years of Python experience", "years of engineering experience"
    re.compile(r"\byears? of (?P<s>.+?) (?:work )?experience\b"),
    # "years have you worked with Java", "years have you been using SQL"
    re.compile(
        r"\byears? (?:have you |do you have )?(?:worked|been working|used|been using|working) "
        r"(?:with|in|on) (?P<s>.+)$"
    ),
    # "proficiency in English", "level of proficiency in German", "fluency in French"
    re.compile(r"\b(?:proficiency|proficient|fluency|fluent)(?: level)? (?:in|with) (?P<s>.+)$"),
)
_YEARS_Q = re.compile(
    r"\bhow (?:many|much) (?:years|yrs)\b|\byears? of\b.*\bexperience\b|\bexperience\b.*\byears?\b"
)
_AT_LEAST = re.compile(
    r"\b(?:at least|minimum(?: of)?|min|more than|over|atleast)\s+(?P<n>\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs)"
    r"|\b(?P<m>\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs)"
    r"|\b(?P<k>\d+(?:\.\d+)?)\s*(?:years?|yrs)\b.*\b(?:or more|plus)\b"
)
_TRAILING = re.compile(
    r"\b(?:do you have|have you|you have|currently|professionally|overall|in total|total)\b.*$"
)


def _clean(subject: str) -> str:
    subject = _TRAILING.sub("", subject)
    subject = _NOISE.sub(" ", subject)
    return re.sub(r"\s+", " ", subject).strip()


def subject(label: str) -> str | None:
    """What an experience or proficiency question is about ("python", "english"); "" for work in general.

    None when the question is not of that kind.
    """
    text = _canon(label)
    for pattern in _SUBJECT_PATTERNS:
        if m := pattern.search(text):
            found = _clean(m.group("s"))
            return "" if found in _GENERAL else found
    if _YEARS_Q.search(text):
        return ""
    return None


def same_subject(a: str, b: str) -> bool:
    """False when two questions are about different skills or languages (never share their answers)."""
    sa, sb = subject(a), subject(b)
    if sa is None and sb is None:
        return True
    return sa == sb


def has_skill(topic: str, skills: Iterable[str]) -> bool:
    """Whether the resume's skills name this topic ("python" in "Python 3", "node js" == "Node.js")."""
    t = _clean(_canon(topic))
    if not t:
        return False
    # Only the topic inside a skill ("python" in "python 3"), never a skill inside a wider topic: "AI" on the
    # resume says nothing about years of "voice AI".
    whole = re.compile(rf"(?:^| ){re.escape(t)}(?: |$)")
    return any((s := _clean(_canon(skill))) and whole.search(s) for skill in skills)


def _range_of(option: str) -> tuple[float, float] | None:
    """The years an option covers: "0-1" (0,1), "3+" (3,inf), "Less than 1 year" (0,1), "5" (5,5)."""
    low = norm(option.replace("+", " plus "))
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", option)]
    if not numbers:
        return (0.0, 0.0) if re.search(r"\b(none|no experience|fresher|entry level)\b", low) else None
    if re.search(r"\b(less than|under|below|upto|up to|fewer than)\b", low) or option.strip().startswith("<"):
        return (
            0.0,
            numbers[0] - 1e-9
            if "less" in low or "under" in low or "below" in low or "fewer" in low
            else numbers[0],
        )
    if re.search(r"\b(plus|more than|over|above|or more|and above|at least)\b", low) or ">" in option:
        return (numbers[0] + (1e-9 if re.search(r"\b(more than|over|above)\b", low) else 0.0), math.inf)
    if len(numbers) >= 2:  # noqa: PLR2004
        return (numbers[0], numbers[1])
    return (numbers[0], numbers[0])


def years_option(options: Iterable[str], years: float) -> str | None:
    """The option whose range holds ``years`` (whole years, as forms count them)."""
    whole = float(math.floor(years))
    hits = [o for o in options if (r := _range_of(o)) is not None and r[0] <= whole <= r[1]]
    return hits[0] if len(hits) == 1 else (min(hits, key=len) if hits else None)


def _years_for(topic: str, years: float | None, skills: Iterable[str]) -> float | None:
    if years is None:
        return None
    if topic == "" or has_skill(topic, skills):
        return years
    return None  # a skill the resume does not show: only the user can say


def experience_answer(  # noqa: PLR0911  (one answer per question shape)
    label: str, options: tuple[str, ...], years: float | None, skills: Iterable[str]
) -> str | bool | None:
    """The answer to an experience question from the profile, or None when it is not one or not known."""
    text = norm(label)
    topic = subject(label)
    if topic is None or not re.search(r"\b(years?|yrs|experience)\b", text):
        return None
    if re.search(r"\b(proficien|fluen|level)\w*\b", text) and "experience" not in text:
        return None  # a language level, not years
    have = _years_for(topic, years, list(skills))
    if have is None:
        return None
    if m := _AT_LEAST.search(re.sub(r"\s+", " ", label.lower())):  # the raw text: "3+" and "2.5" survive
        needed = float(m.group("n") or m.group("m") or m.group("k"))
        strictly = bool(re.search(r"\b(more than|over)\b", text))
        return have > needed if strictly else have >= needed
    if options and all(re.fullmatch(r"\s*(yes|no)\b.*", o, re.I) for o in options):
        # "Do you have experience with Python?": yes when the resume lists it; in general, any years at all
        return True if topic else have > 0
    if options:
        return years_option(options, have)
    if re.search(r"^(do|have|are|did) you\b", text):
        return True if topic else have > 0  # a yes/no question typed as free text
    return str(math.floor(have))
