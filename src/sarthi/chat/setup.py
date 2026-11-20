"""The /setup conversation: resume upload, a few questions, a summary, then the saved profile.

Progress is kept in the database (``app_state``), so a restart in the middle loses nothing. Every value the AI
suggests from the resume is shown and must be confirmed ("ok") or replaced by the user.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sarthi.ai.answers import interpret_list
from sarthi.ai.client import AiClient
from sarthi.ai.resume import read_resume
from sarthi.chat.telegram.api import TelegramApi, buttons
from sarthi.clock import Clock, utcnow
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.jobs.geo import country_name
from sarthi.jobs.models import Salary
from sarthi.profile.answers import (
    edit_list,
    is_none,
    is_simple_answer,
    looks_like_instruction,
    parse_countries,
    parse_remote,
    parse_salary,
    parse_years,
    reconcile,
)
from sarthi.profile.models import Preferences, Profile, RemotePreference
from sarthi.profile.resume import MAX_UPLOAD_BYTES, ResumeError, extract_text
from sarthi.profile.store import load_profile, save_profile
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, Priority, TaskQueue

log = logging.getLogger(__name__)

STATE_KEY = "setup.state"
REMATCH_WINDOW = timedelta(days=7)
MAX_ROLES = 10
MAX_SKILLS = 40
SHOWN_SKILLS = 25
_OK = frozenset({"ok", "okay", "yes", "y", "keep", "same"})


class Step(StrEnum):
    """Questions, in order."""

    RESUME = "resume"
    ROLES = "roles"
    HOME = "home"
    COUNTRIES = "countries"
    VISA_FREE = "visa_free"
    REMOTE = "remote"
    YEARS = "years"
    SKILLS = "skills"
    SALARY = "salary"
    CONFIRM = "confirm"


_ORDER = list(Step)
_REMOTE_LABELS = {
    RemotePreference.OK: "Remote, hybrid or on-site",
    RemotePreference.ONLY: "Remote only",
    RemotePreference.NO: "No remote (on-site or hybrid)",
}
QUESTIONS: dict[Step, str] = {
    Step.ROLES: "💼 Which <b>job titles</b> should I look for? (comma-separated)",
    Step.HOME: "🏠 Which <b>country do you live in</b>?",
    Step.COUNTRIES: (
        "🌍 Any <b>other countries</b> you'd work in on-site or hybrid? "
        "(e.g. UK, UAE, Europe) — or <b>none</b>"
    ),
    Step.VISA_FREE: (
        "🛂 Besides {home}, where can you work <b>without a visa</b>? "
        "(e.g. an EU passport → Europe) — or <b>none</b>"
    ),
    Step.REMOTE: "🏡 What about <b>remote work</b>?",
    Step.YEARS: "📅 How many <b>years of work experience</b> do you have?",
    Step.SKILLS: "🛠 Your main <b>skills</b>? (comma-separated)",
    Step.SALARY: ("💰 <b>Minimum salary</b> you'd accept? e.g. 12 LPA, 80k GBP, USD 120000 — or <b>skip</b>"),
}
START_TEXT = (
    "Let's set up your profile (about 2 minutes). Send /cancel to stop.\n\n"
    "📄 First, send me your <b>resume</b> as a PDF or Word (.docx) file."
)
SAVED_TEXT = (
    "✅ Saved. I'm matching {count} recent job(s) against your profile, and every new one from now on.\n"
    "Strong matches come right away, the rest in your daily digest. /profile shows your profile."
)
EDIT_HINT = "Reply <b>ok</b> to use it, or e.g. <i>ok, remove X, add Y</i>, or type your own."


def _salary_text(draft: dict[str, Any]) -> str:
    salary = draft.get("salary")
    return f"{salary['currency']} {salary['minimum']:,.0f} / {salary['period']}" if salary else "not set"


_UNDERSTOOD: dict[Step, tuple[str, Callable[[dict[str, Any]], str]]] = {
    Step.ROLES: ("Roles", lambda d: ", ".join(d.get("roles") or []) or "none"),
    Step.HOME: ("Lives in", lambda d: country_name(d["home"])),
    Step.COUNTRIES: ("Other countries", lambda d: _names(d.get("countries") or [])),
    Step.VISA_FREE: ("No visa needed in (besides home)", lambda d: _names(d.get("visa_free") or [])),
    Step.REMOTE: ("Remote", lambda d: _REMOTE_LABELS[RemotePreference(d["remote"])]),
    Step.YEARS: ("Experience", lambda d: f"{d['years']:g} years"),
    Step.SKILLS: ("Skills", lambda d: ", ".join(d.get("skills") or []) or "none"),
    Step.SALARY: ("Minimum salary", _salary_text),
}
_LIST_STEPS = {Step.ROLES: "roles", Step.SKILLS: "skills"}


def _current_list(state: dict[str, Any], key: str) -> list[str]:
    """The list an edit applies to: the user's own answer (e.g. after Change), else the suggestion."""
    return list(state["draft"].get(key) or state["suggest"].get(key) or [])


UNKNOWN_PLACES = "I don't know {places}. Use country names (or 'none')."


def _names(codes: list[str]) -> str:
    """Country names, alphabetically."""
    return ", ".join(sorted(country_name(c) for c in codes)) or "none"


def summary(profile: Profile) -> str:
    """The profile as a short message."""
    p = profile.preferences
    salary = p.min_salary
    salary_text = "not set"
    if salary is not None and salary.minimum:
        salary_text = f"{salary.currency} {salary.minimum:,.0f} / {salary.period.value}"
    years = f"{p.years_experience:g}" if p.years_experience is not None else "not set"
    lines = [
        "<b>Your Sarthi profile</b>",
        f"Roles: {escape(', '.join(p.roles))}",
        f"Lives in: {escape(country_name(p.home_country))}",
        f"Would work in: {escape(_names(sorted(p.work_countries)))}",
        f"No visa needed in: {escape(_names(sorted(p.visa_free_countries)))}",
        f"Remote: {_REMOTE_LABELS[p.remote]}",
        f"Experience: {years} years",
        f"Skills: {escape(', '.join((*profile.skills, *p.keywords)) or 'none')}",
        f"Minimum salary: {escape(salary_text)}",
        f"Resume: {'saved' if profile.resume_text else 'not uploaded'}",
    ]
    return "\n".join(lines)


class SetupFlow:
    """Runs the setup conversation for the paired user."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        api: TelegramApi,
        chats: ChatStore,
        ai: AiClient,
        profile_file: Path,
        *,
        jobs: JobStore,
        queue: TaskQueue,
        clock: Clock = utcnow,
        load: Callable[[Path], Profile | None] = load_profile,
    ) -> None:
        """Ask questions through ``api``; save the profile to ``profile_file``; re-match recent jobs."""
        self.api = api
        self.chats = chats
        self.ai = ai
        self.profile_file = profile_file
        self.jobs = jobs
        self.queue = queue
        self._clock = clock
        self._load = load

    # ---- state ------------------------------------------------------------------------------------------

    def _state(self) -> dict[str, Any] | None:
        raw = self.chats.get_state(STATE_KEY)
        return json.loads(raw) if raw else None

    def _save_state(self, state: dict[str, Any] | None) -> None:
        self.chats.set_state(STATE_KEY, json.dumps(state) if state is not None else None)

    @property
    def active(self) -> bool:
        """True while a setup conversation is in progress."""
        return self._state() is not None

    # ---- entry points -----------------------------------------------------------------------------------

    async def start(self, chat_id: int) -> None:
        """Begin (or restart) setup; current profile values become the suggestions."""
        current = self._load(self.profile_file)
        suggest: dict[str, Any] = {}
        draft: dict[str, Any] = {}
        if current is not None:
            prefs = current.preferences
            suggest = {
                "roles": list(prefs.roles),
                "skills": [*current.skills, *prefs.keywords],
                "years": prefs.years_experience,
                "home": prefs.home_country,
            }
            draft["resume_text"] = current.resume_text
        self._save_state({"step": Step.RESUME, "draft": draft, "suggest": suggest})
        keep = " Or reply <b>skip</b> to keep the one you sent before." if draft.get("resume_text") else ""
        await self.api.send_message(chat_id, START_TEXT + (keep or " Or reply <b>skip</b>."))

    async def show(self, chat_id: int) -> None:
        """Send the saved profile, or how to create one."""
        profile = self._load(self.profile_file)
        if profile is None:
            await self.api.send_message(chat_id, "You have no profile yet. Send /setup to create one.")
            return
        await self.api.send_message(chat_id, summary(profile) + "\n\n/setup to change it.")

    async def cancel(self, chat_id: int) -> None:
        """Stop setup without saving."""
        self._save_state(None)
        await self.api.send_message(chat_id, "Setup cancelled. Nothing was changed. /setup to start again.")

    async def on_document(self, chat_id: int, document: dict[str, Any]) -> None:
        """A file arrived during setup: read it as the resume."""
        state = self._state()
        if state is None or state["step"] != Step.RESUME:
            await self.api.send_message(chat_id, "To update your resume, send /setup first.")
            return
        if int(document.get("file_size") or 0) > MAX_UPLOAD_BYTES:
            await self.api.send_message(chat_id, "That file is over 5 MB. Please send a smaller resume.")
            return
        try:
            data = await self.api.download(str(document.get("file_id", "")), MAX_UPLOAD_BYTES)
            text = extract_text(data)
        except ResumeError as error:
            await self.api.send_message(chat_id, str(error))
            return
        except PermanentError:
            await self.api.send_message(chat_id, "I couldn't download that file. Please send it again.")
            return
        state["draft"]["resume_text"] = text
        await self.api.send_message(chat_id, "✓ Resume read. Looking for your skills…")
        facts = await read_resume(self.ai, text)
        if facts is not None:
            suggest = state["suggest"]
            suggest["roles"] = facts.roles or suggest.get("roles", [])
            suggest["skills"] = facts.skills
            suggest["years"] = facts.years_experience
        else:
            await self.api.send_message(chat_id, "(No AI model answered, so I'll just ask you.)")
        await self._advance(chat_id, state)

    async def on_text(self, chat_id: int, text: str) -> None:
        """An answer to the current question."""
        state = self._state()
        if state is None:
            return
        step = Step(state["step"])
        error: str | None
        if step in _LIST_STEPS and not is_simple_answer(text):
            error = await self._answer_with_ai(chat_id, step, state, text.strip())
        else:
            answer: Callable[[dict[str, Any], str], str | None] = getattr(self, f"_answer_{step}")
            error = answer(state, text.strip())
        if error:
            await self.api.send_message(chat_id, error)
            return
        if understood := self._understood(step, state["draft"]):
            change = buttons([("✏️ Change", f"setup:back:{step}")])
            await self.api.send_message(chat_id, understood, change)
        await self._advance(chat_id, state)

    async def on_button(self, chat_id: int, data: str) -> None:
        """A setup button: ``setup:remote:<value>``, ``setup:save`` or ``setup:restart``."""
        state = self._state()
        if state is None:
            return
        if data.startswith("setup:remote:") and state["step"] == Step.REMOTE:
            await self.on_text(chat_id, data.removeprefix("setup:remote:"))
        elif data == "setup:save" and state["step"] == Step.CONFIRM:
            await self._finish(chat_id, state)
        elif data == "setup:restart":
            await self.start(chat_id)
        elif data.startswith("setup:back:"):
            await self._go_back(chat_id, state, data.removeprefix("setup:back:"))

    async def _go_back(self, chat_id: int, state: dict[str, Any], step_name: str) -> None:
        """Ask an earlier question again (later answers are kept and shown again at the summary)."""
        if step_name not in Step.__members__.values() or step_name == Step.RESUME:
            return
        step = Step(step_name)
        if _ORDER.index(step) > _ORDER.index(Step(state["step"])):
            return
        current = state["step"]
        state["step"] = step
        state["back_to"] = state.get("back_to") or current  # after fixing, return to where the user was
        self._save_state(state)
        await self._ask(chat_id, state)

    # ---- answers (each returns an error message, or None when accepted) ---------------------------------

    async def _answer_with_ai(self, chat_id: int, step: Step, state: dict[str, Any], text: str) -> str | None:
        """Free-form edits ("remove fintech, add ML engineer instead"): the AI works out the new list."""
        key = _LIST_STEPS[step]
        current = _current_list(state, key)
        if self.ai.available:
            await self.api.send_message(chat_id, "🤔 Understanding your answer…")
        items = await interpret_list(self.ai, QUESTIONS[step], current, text)
        if items is not None:
            items = reconcile(current, items, text)  # rules catch what a small model drops or misses
        if items is None or any(looks_like_instruction(item) for item in items):
            answer: Callable[[dict[str, Any], str], str | None] = getattr(self, f"_answer_{step}")
            return answer(state, text)  # no AI, or it could not tell: the rules try
        if step is Step.ROLES and not items:
            return "Please name at least one job title, e.g. Backend Engineer, Data Analyst."
        state["draft"][key] = items[: MAX_ROLES if step is Step.ROLES else MAX_SKILLS]
        return None

    @staticmethod
    def _suggested(state: dict[str, Any], key: str, text: str) -> Any:
        """The suggestion when the user replied "ok", else None."""
        return state["suggest"].get(key) if text.lower() in _OK else None

    def _answer_resume(self, state: dict[str, Any], text: str) -> str | None:
        if is_none(text):
            return None
        return "Please send the resume as a file (📎 → File), or reply skip."

    def _answer_roles(self, state: dict[str, Any], text: str) -> str | None:
        edit = edit_list(text, _current_list(state, "roles"))
        if edit.error:
            return edit.error
        if not edit.items:
            return "Please name at least one job title, e.g. Backend Engineer, Data Analyst."
        state["draft"]["roles"] = list(edit.items[:MAX_ROLES])
        return None

    def _answer_home(self, state: dict[str, Any], text: str) -> str | None:
        suggested = self._suggested(state, "home", text)
        found = frozenset({suggested}) if suggested else parse_countries(text).codes
        if len(found) != 1:
            return "Please name one country, e.g. India or United Kingdom."
        state["draft"]["home"] = next(iter(found))
        return None

    def _countries(self, state: dict[str, Any], text: str, key: str) -> str | None:
        parsed = parse_countries(text)
        if parsed.unknown:
            return UNKNOWN_PLACES.format(places=escape(", ".join(parsed.unknown)))
        state["draft"][key] = sorted(parsed.codes)
        return None

    def _answer_countries(self, state: dict[str, Any], text: str) -> str | None:
        return self._countries(state, text, "countries")

    def _answer_visa_free(self, state: dict[str, Any], text: str) -> str | None:
        return self._countries(state, text, "visa_free")

    def _answer_remote(self, state: dict[str, Any], text: str) -> str | None:
        preference = parse_remote(text)
        if preference is None:
            return "Please tap one of the buttons."
        state["draft"]["remote"] = preference.value
        return None

    def _answer_years(self, state: dict[str, Any], text: str) -> str | None:
        suggested = self._suggested(state, "years", text)
        years = suggested if suggested is not None else parse_years(text)
        if years is None:
            return "Please reply with a number of years, e.g. 3 or 0 for a fresher."
        state["draft"]["years"] = years
        return None

    def _answer_skills(self, state: dict[str, Any], text: str) -> str | None:
        edit = edit_list(text, _current_list(state, "skills"))
        if edit.error:
            return edit.error
        state["draft"]["skills"] = list(edit.items[:MAX_SKILLS])
        return None

    def _answer_salary(self, state: dict[str, Any], text: str) -> str | None:
        if is_none(text):
            state["draft"]["salary"] = None
            return None
        salary = parse_salary(text, state["draft"]["home"])
        if salary is None:
            return "I didn't get that. Try e.g. 12 LPA, 80k GBP, USD 120000 — or skip."
        state["draft"]["salary"] = salary.model_dump(mode="json")
        return None

    def _answer_confirm(self, state: dict[str, Any], text: str) -> str | None:
        return "Tap ✅ Save or 🔄 Start over."

    # ---- flow ---------------------------------------------------------------------------------------------

    @staticmethod
    def _profile(state: dict[str, Any]) -> Profile:
        draft = state["draft"]
        prefs = Preferences(
            roles=tuple(draft["roles"]),
            home_country=draft["home"],
            countries=frozenset(draft.get("countries", [])),
            authorized_countries=frozenset(draft.get("visa_free", [])),
            remote=RemotePreference(draft.get("remote", RemotePreference.OK)),
            years_experience=draft.get("years"),
            min_salary=Salary.model_validate(draft["salary"]) if draft.get("salary") else None,
        )
        skills = tuple(draft.get("skills", []))
        return Profile(preferences=prefs, skills=skills, resume_text=draft.get("resume_text", ""))

    @staticmethod
    def _understood(step: Step, draft: dict[str, Any]) -> str | None:
        """How Sarthi read the answer, so a misunderstanding is caught right away."""
        shown = _UNDERSTOOD.get(step)
        return f"✓ {shown[0]}: <b>{escape(shown[1](draft))}</b>" if shown else None

    async def _advance(self, chat_id: int, state: dict[str, Any]) -> None:
        if back_to := state.pop("back_to", None):  # a corrected answer returns to where the user was
            state["step"] = back_to
            self._save_state(state)
            await self._ask(chat_id, state)
            return
        state["step"] = _ORDER[_ORDER.index(Step(state["step"])) + 1]
        self._save_state(state)
        await self._ask(chat_id, state)

    @staticmethod
    def _suggestion(step: Step, suggest: dict[str, Any]) -> str:
        """The resume-based suggestion shown under a question, if any."""
        shown = {
            Step.ROLES: ", ".join(suggest.get("roles") or []),
            Step.HOME: country_name(suggest["home"]) if suggest.get("home") else "",
            Step.YEARS: f"{suggest['years']:g} years" if suggest.get("years") is not None else "",
            Step.SKILLS: ", ".join((suggest.get("skills") or [])[:SHOWN_SKILLS]),
        }.get(step, "")
        return f"\n\nSuggested: <b>{escape(shown)}</b>\n{EDIT_HINT}" if shown else ""

    async def _ask(self, chat_id: int, state: dict[str, Any]) -> None:
        step = Step(state["step"])
        if step is Step.CONFIRM:
            await self._confirm(chat_id, state)
            return
        if step is Step.REMOTE:
            choices = [[(label, f"setup:remote:{value.value}")] for value, label in _REMOTE_LABELS.items()]
            await self.api.send_message(chat_id, QUESTIONS[step], buttons(*choices))
            return
        home = escape(country_name(state["draft"].get("home", "")))
        question = QUESTIONS[step].format(home=home)
        await self.api.send_message(chat_id, question + self._suggestion(step, state["suggest"]))

    async def _confirm(self, chat_id: int, state: dict[str, Any]) -> None:
        try:
            profile = self._profile(state)
        except (ValidationError, KeyError) as error:
            log.warning("setup produced an invalid profile: %s", error)
            await self.api.send_message(chat_id, "Something didn't add up. Let's start again.")
            await self.start(chat_id)
            return
        choices = buttons([("✅ Save", "setup:save"), ("🔄 Start over", "setup:restart")])
        await self.api.send_message(chat_id, summary(profile), choices)

    async def _finish(self, chat_id: int, state: dict[str, Any]) -> None:
        save_profile(self.profile_file, self._profile(state))
        self._save_state(None)
        recent = self.jobs.ids_seen_since(self._clock() - REMATCH_WINDOW)
        for job_id in recent:
            self.queue.enqueue(names.MATCH, job_id, priority=Priority.BACKGROUND)
        await self.api.send_message(chat_id, SAVED_TEXT.format(count=len(recent)))
