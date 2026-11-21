"""Reading a reply about a job application with the AI: what it is, when, and what to do.

Used only for emails the rules could not sort (interviews, offers, tests, anything unclear). The email is
untrusted text: it is fenced as data, contact details are removed first, and the answer must pick a kind
from a fixed list. Adapted from ApplyPilot's tracking classifier prompt (AGPL-3.0).
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.privacy import scrub
from sarthi.ai.prompts import Prompt

log = logging.getLogger(__name__)

EMAIL_CHARS = 4000
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class ReplyAnswer(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["confirmation", "rejection", "interview", "assessment", "offer", "other", "noise"]
    confidence: float = Field(ge=0, le=1)
    summary: Short = ""
    dates: list[Short] = Field(
        default_factory=list, max_length=5
    )  # "2026-12-02 15:00 IST: technical interview"
    actions: list[Short] = Field(default_factory=list, max_length=5)  # what the candidate should do


REPLY_PROMPT = Prompt(
    name="job_reply",
    version="1",
    instructions="""
<email> is a message the candidate received. Decide what it is:
- "confirmation": the application was received
- "rejection": the candidate was not selected
- "interview": an interview invitation or scheduling
- "assessment": a test, take-home task or a request for more information
- "offer": a job offer or compensation discussion
- "other": about the application, none of the above
- "noise": marketing, job alerts, newsletters, unrelated

Return JSON: {"kind": "...", "confidence": 0-1, "summary": "one short sentence for the candidate",
"dates": ["YYYY-MM-DD HH:MM (timezone): what"], "actions": ["what the candidate should do, with deadline"]}.
Only dates and actions the email states; never invent them.
""",
)


async def read_reply(client: AiClient, sender: str, subject: str, body: str) -> ReplyAnswer | None:
    """The AI's reading of one email, or None when no model answered."""
    if not client.available:
        return None
    text = f"From: {sender}\nSubject: {subject}\n\n{body[:EMAIL_CHARS]}"
    try:
        return await client.structured(REPLY_PROMPT, ReplyAnswer, email=scrub(text))
    except AiError as error:
        log.info("reply not read by AI: %s", error)
        return None
