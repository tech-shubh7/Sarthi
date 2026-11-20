from pathlib import Path
from typing import Any

from sarthi.app import repair_saved_profile
from sarthi.profile.answers import looks_like_instruction
from sarthi.profile.models import Preferences, Profile
from sarthi.profile.repair import clean_list, repair
from sarthi.profile.store import load_profile, save_profile

# Saved by the first buggy setup runs on 2026-11-20.
JUNK_ROLES = [
    "Backend Engineer", "AI Engineer", "Node.js Developer", "Fintech Application Developer",
    "Microservices Developer", "backend AI engineer",
    "software developer associate software developer also", "Expect Fintech", "microservices one",
]  # fmt: skip


def profile(roles: list[str], skills: list[str]) -> Profile:
    data: dict[str, Any] = {"roles": roles, "home_country": "IN"}
    return Profile(preferences=Preferences.model_validate(data), skills=tuple(skills))


def test_real_junk_is_removed_and_real_names_survive() -> None:
    fixed = repair(profile(JUNK_ROLES, ["Use skills"]))
    assert "Expect Fintech" not in fixed.preferences.roles
    assert "microservices" in fixed.preferences.roles  # "microservices one" trimmed
    assert "software developer associate software developer" in fixed.preferences.roles  # "also" trimmed
    assert fixed.skills == ()


def test_real_names_with_instruction_like_words_are_kept() -> None:
    names = [
        "REST API design",
        "No-code automation",
        "Use case analysis",
        "User research",
        "Okta",
        "Addressable markets",
    ]
    assert clean_list(names) == names
    assert not any(looks_like_instruction(name) for name in names)


def test_clean_profiles_are_returned_unchanged() -> None:
    clean = profile(["Backend Engineer"], ["Python", "REST API design"])
    assert repair(clean) is clean


def test_roles_are_never_emptied() -> None:
    fixed = repair(profile(["Use these", "ok"], []))
    assert fixed.preferences.roles == ("Use these", "ok")


def test_saved_profile_is_cleaned_on_start(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    save_profile(path, profile(JUNK_ROLES, ["Use skills", "Python"]))
    repair_saved_profile(path)
    saved = load_profile(path)
    assert saved is not None
    assert saved.skills == ("Python",)
    repair_saved_profile(tmp_path / "missing.json")  # no profile: nothing to do
