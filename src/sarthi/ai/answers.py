"""Understanding a free-form list edit with the AI ("remove ML engineer, add data engineer instead").

The model does not rewrite the list. It answers two things: which of the *current items* to keep (by exact
text, so "ML engineer" can be matched to "Machine Learning Engineer" by the model, and nothing outside the
list can be "kept"), and which new items to add. Code then builds the list from those answers and checks
them; it never second-guesses the model with word patterns. Plain answers ("ok", a plain list) and the
tap-to-remove buttons never reach this module.
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


class ListEditAnswer(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    keep: list[Item] = Field(default_factory=list, max_length=60)
    add: list[Item] = Field(default_factory=list, max_length=40)
    unclear: bool = False


EDIT_LIST_PROMPT = Prompt(
    name="edit_list",
    version="3",
    instructions="""
The user is answering a question in a short setup chat. <current_list> is the list shown to them, one item per
line (it may be empty). <user_answer> is what they replied. Work out what they want.

Return JSON with:
- "keep": the items of <current_list> to keep, copied EXACTLY as written there. Drop every item they asked
  to remove, even when they named it loosely, by abbreviation or with typos ("ML engineer" = "Machine
  Learning Engineer", "the node one" = "Node.js Developer", "expect fintech" = except fintech). If they gave
  a whole new list or said "only X", keep only what they asked for. An answer that is only a list of
  names, with no word like add, also, keep, ok or remove, is a whole new list: keep nothing from
  <current_list> unless it is named in it. Otherwise, items they did not mention stay.
- "add": new items they asked for that are not already in <current_list>, written properly capitalised.
  Never put instruction words (remove, add, ok, keep, except) in an item.
- "unclear": true only if you really cannot tell what they want (then keep everything and add nothing).
""",
)


def _exact(items: list[str], current: list[str]) -> list[str]:
    """Kept items mapped back to the current list's own spelling; anything not in the list is dropped."""
    wanted = {item.lower() for item in items}
    return [item for item in current if item.lower() in wanted]


async def interpret_list(
    client: AiClient, question: str, current: list[str], answer: str
) -> list[str] | None:
    """The list the user wants, or None when no AI could answer or it could not tell (rules take over)."""
    if not client.available:
        return None
    try:
        result = await client.structured(
            EDIT_LIST_PROMPT,
            ListEditAnswer,
            urgent=True,
            question=question,
            current_list="\n".join(current) or "(empty)",
            user_answer=answer[:MAX_ANSWER_CHARS],
        )
    except AiError as error:
        log.info("setup answer not interpreted by AI: %s", error)
        return None
    if result.unclear:
        return None
    kept = _exact(result.keep, current)
    known = {item.lower() for item in kept}
    added: list[str] = []
    for item in result.add:
        if item.lower() not in known:
            known.add(item.lower())
            added.append(item)
    return kept + added
