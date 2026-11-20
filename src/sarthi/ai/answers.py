"""Understanding a free-form setup answer with the AI ("remove fintech and node, add ML engineer instead").

Simple answers ("ok", a plain list) are handled by fast rules in ``sarthi.profile.answers``; this is for
everything else. The model returns the complete new list; Sarthi shows it back to the user, who can change it
with one tap, so a misreading never goes unnoticed.
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.prompts import Prompt

log = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 1000
Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class ListAnswer(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    items: list[Item] = Field(default_factory=list, max_length=40)
    unclear: bool = False


EDIT_LIST_PROMPT = Prompt(
    name="edit_list",
    version="1",
    instructions="""
The user is answering a question in a short setup chat. <current_list> is the list shown to them (it may be
empty). <user_answer> is what they replied. Work out the complete list they want now.

Rules:
- Apply every change they ask for: remove items (even if named loosely, e.g. "fintech" or "the node one"),
  add new items, replace items, keep only some, or give a whole new list.
- Items they did not mention stay, unless they gave a whole new list or said to keep only some.
- Fix obvious typos in what they wrote (e.g. "expect" meaning "except"), and write each item properly
  capitalised, e.g. "ML Engineer".
- Never put instruction words (remove, add, ok, keep, except) inside an item.
- If you really cannot tell what they want, return the current list unchanged with "unclear": true.

Return JSON: {"items": [...], "unclear": false}
""",
)


async def interpret_list(
    client: AiClient, question: str, current: list[str], answer: str
) -> list[str] | None:
    """The list the user wants, or None when no AI could answer or it could not tell (rules take over)."""
    if not client.available:
        return None
    try:
        result = await client.structured(
            EDIT_LIST_PROMPT,
            ListAnswer,
            question=question,
            current_list="\n".join(f"- {item}" for item in current) or "(empty)",
            user_answer=answer[:MAX_ANSWER_CHARS],
        )
    except AiError as error:
        log.info("setup answer not interpreted by AI: %s", error)
        return None
    if result.unclear:
        return None
    unique: dict[str, str] = {}
    for item in result.items:
        unique.setdefault(item.lower(), item)
    return list(unique.values())
