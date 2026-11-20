"""What a chat message is: an answer, a correction, a question, small talk or off-topic.

Fast rules decide the clear cases instantly; only messages they cannot place go to the AI
(``sarthi.ai.intent``), which must choose from the same fixed kinds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sarthi.ai.client import AiClient
from sarthi.ai.intent import Field, Kind, classify

_SMALLTALK = re.compile(
    r"^(?:hi+|hello|hey+|hii+|namaste|yo|good\s+(?:morning|afternoon|evening|night)|thanks?(?:\s+you)?|thank\s+you|"
    r"thx|ty|cool|nice|great|awesome|wow|wait|one\s+(?:sec|second|min|minute|moment)|hold\s+on|hmm+|ok\s+wait|"
    r"bye|good\s*bye|see\s+you|lol|haha+|👍|🙏|😊|🙂|👋)[\s!.?]*$",
    re.IGNORECASE,
)
_QUESTION = re.compile(
    r"\?\s*$|^(?:why|what|how|who|where|when|which|can\s+you|could\s+you|do\s+you|does|is\s+it|are\s+you|"
    r"will\s+you|should\s+i|do\s+i)\b",
    re.IGNORECASE,
)
_CHANGE = re.compile(
    r"\b(?:actually|change|update|set|make|add|remove|also|instead|now|correct|fix|wrong|switch|moved?|prefer|"
    r"not|except|without|replace|delete|drop)\b",
    re.IGNORECASE,
)
_OFF_TOPIC_WORDS = re.compile(
    r"\b(?:joke|poem|weather|news|story|song|recipe|movie|cricket|football|match\s+score|homework|essay)\b",
    re.IGNORECASE,
)
_CHATTY = re.compile(r"\b(?:you|your|tell\s+me|write|give\s+me)\b", re.IGNORECASE)
# Checked in order: the first field whose words appear wins ("home country" before "country").
_FIELD_WORDS: tuple[tuple[Field, re.Pattern[str]], ...] = (
    (
        Field.HOME,
        re.compile(r"\b(?:i\s+live|live\s+in|home\s+country|based\s+in|moved\s+to|living\s+in)\b", re.I),
    ),
    (Field.VISA_FREE, re.compile(r"\b(?:visa|passport|work\s+permit|citizen(?:ship)?)\b", re.I)),
    (Field.COUNTRIES, re.compile(r"\b(?:countr(?:y|ies)|relocat\w*|work\s+in|abroad)\b", re.I)),
    (Field.REMOTE, re.compile(r"\b(?:remote|hybrid|on-?site|office|wfh|work\s+from\s+home)\b", re.I)),
    (Field.SALARY, re.compile(r"\b(?:salary|ctc|lpa|pay|package|compensation|stipend)\b", re.I)),
    (Field.YEARS, re.compile(r"\b(?:years?|yrs?|experience)\b", re.I)),
    (Field.SKILLS, re.compile(r"\b(?:skills?|tech\s+stack|technolog(?:y|ies)|tools?|languages?)\b", re.I)),
    (Field.ROLES, re.compile(r"\b(?:roles?|job\s+titles?|titles?|positions?|designations?)\b", re.I)),
)
MAX_ANSWER_WORDS = 40


@dataclass(frozen=True, slots=True)
class Route:
    """What to do with a message."""

    kind: Kind
    field: Field | None = None


def mentioned_field(text: str) -> Field | None:
    """The profile field a message talks about, if any."""
    return next((field for field, words in _FIELD_WORDS if words.search(text)), None)


def route_by_rules(text: str, asking: Field | None) -> Route | None:
    """The route when the rules are sure, else None. ``asking`` is the field of the question being asked."""
    text = text.strip()
    field = mentioned_field(text)
    is_question = _QUESTION.search(text) is not None
    chatty_answer = (
        asking is not None
        and not is_question
        and (_CHATTY.search(text) is not None or len(text.split()) > MAX_ANSWER_WORDS)
    )
    unsure = _OFF_TOPIC_WORDS.search(text) is not None or chatty_answer
    decided: Route | None = None
    if _SMALLTALK.match(text):
        decided = Route(Kind.SMALLTALK)
    elif field is not None and field != asking and _CHANGE.search(text):
        decided = Route(Kind.CORRECTION, field)
    elif unsure:
        decided = None  # possibly off-topic: the AI decides
    elif is_question:
        decided = Route(Kind.QUESTION, field)
    elif asking is not None:
        decided = Route(Kind.ANSWER, asking)
    return decided


async def route(client: AiClient, text: str, asking: Field | None, question: str) -> Route:
    """Rules first; the AI for the rest. Falls back to "answer" (or "off topic" when nothing was asked)."""
    if (decided := route_by_rules(text, asking)) is not None:
        return decided
    verdict = await classify(client, question, text)
    if verdict is None:
        return Route(Kind.ANSWER, asking) if asking is not None else Route(Kind.OFF_TOPIC)
    if verdict.kind is Kind.ANSWER and asking is None:
        return Route(Kind.OFF_TOPIC)
    if verdict.kind is Kind.CORRECTION and verdict.field is None:
        field = mentioned_field(text)
        return Route(Kind.CORRECTION, field) if field else Route(Kind.QUESTION)
    return Route(verdict.kind, verdict.field if verdict.kind is Kind.CORRECTION else asking)
