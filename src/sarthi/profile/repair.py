"""Cleaning junk that older versions saved into a profile, e.g. the instruction "Use skills" as a skill.

Roles and skills are passed through the same checks a new answer gets; anything that is really an instruction
("Use skills", "Expect Fintech") or not a name is dropped, and filler around a name ("microservices one") is
trimmed. A profile is never left without roles: if every role is junk, they are kept for the user to fix.
"""

from __future__ import annotations

from sarthi.profile.answers import clean_item, looks_like_instruction, tidy_item
from sarthi.profile.models import Profile


def clean_list(items: tuple[str, ...] | list[str]) -> list[str]:
    """Items fit to keep, tidied and de-duplicated, in their original order."""
    kept: dict[str, str] = {}
    for raw in items:
        item = clean_item(tidy_item(raw))
        if item is not None and not looks_like_instruction(item):
            kept.setdefault(item.lower(), item)
    return list(kept.values())


def repair(profile: Profile) -> Profile:
    """``profile`` with junk roles and skills removed (the same object when nothing needed fixing)."""
    prefs = profile.preferences
    roles = clean_list(prefs.roles) or list(prefs.roles)
    skills = clean_list(profile.skills)
    keywords = clean_list(prefs.keywords)
    if (tuple(roles), tuple(skills), tuple(keywords)) == (prefs.roles, profile.skills, prefs.keywords):
        return profile
    fixed_prefs = prefs.model_copy(update={"roles": tuple(roles), "keywords": tuple(keywords)})
    return profile.model_copy(update={"preferences": fixed_prefs, "skills": tuple(skills)})
