"""Deciding what a chat message is, when the fast rules in ``sarthi.chat.understand`` are not sure.

The model must pick one kind from a fixed list (and a profile field for corrections), so it cannot drift.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.prompts import Prompt

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000


class Kind(StrEnum):
    """What a message is."""

    ANSWER = "answer"  # answers the question being asked
    CORRECTION = "correction"  # changes an earlier profile answer
    QUESTION = "question"  # asks about Sarthi or the setup
    SMALLTALK = "smalltalk"  # greeting, thanks, "wait"
    OFF_TOPIC = "off_topic"  # unrelated to the job search


class Field(StrEnum):
    """Profile fields a correction can change."""

    ROLES = "roles"
    HOME = "home"
    COUNTRIES = "countries"
    VISA_FREE = "visa_free"
    REMOTE = "remote"
    YEARS = "years"
    SKILLS = "skills"
    SALARY = "salary"


class IntentAnswer(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    kind: Kind
    field: Field | None = None


INTENT_PROMPT = Prompt(
    name="intent",
    version="1",
    instructions="""
You route messages for Sarthi, a job-search assistant chatting with its user. <question> is what Sarthi last
asked ("none" if nothing). <user_answer> is the user's message. Decide what the message is:

- "answer": it answers <question>
- "correction": it changes one of the user's profile settings, other than <question>; set "field" to one of
  roles (job titles), home (country they live in), countries (other countries they'd work in), visa_free
  (where they need no visa), remote (remote/hybrid/office preference), years (experience), skills, salary
- "question": it asks something about Sarthi, the setup or why something is asked
- "smalltalk": greeting, thanks, "wait", or similar
- "off_topic": anything unrelated to their job search (jokes, weather, writing tasks, general knowledge)

Return JSON: {"kind": "...", "field": null}
""",
)


async def classify(client: AiClient, question: str, message: str) -> IntentAnswer | None:
    """The message's kind, or None when no AI model could answer."""
    if not client.available:
        return None
    try:
        return await client.structured(
            INTENT_PROMPT, IntentAnswer, question=question or "none", user_answer=message[:MAX_MESSAGE_CHARS]
        )
    except AiError as error:
        log.info("message not classified by AI: %s", error)
        return None
