"""The profile conversation: /setup (resume, a few questions, summary) and corrections at any time.

Progress is kept in the database (``app_state``), so a restart in the middle loses nothing. Every answer is
read the same way whether it answers the current question or corrects an earlier one ("actually add UAE to
my countries"): fast rules for plain answers, the AI checked by rules for free-form edits. Sarthi repeats back
what it understood with a Change (during setup) or Undo (afterwards) button.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sarthi.ai.answers import interpret_list
from sarthi.ai.client import AiClient
from sarthi.ai.intent import Field
from sarthi.ai.resume import read_resume
from sarthi.chat.telegram.api import TelegramApi, buttons
from sarthi.clock import Clock, utcnow
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.jobs.geo import country_name, first_country
from sarthi.jobs.models import Salary
from sarthi.profile.answers import (
    ALL_COUNTRIES,
    clean_item,
    edit_list,
    find_salary,
    is_none,
    is_salary_skip,
    is_simple_answer,
    looks_like_instruction,
    normalize_answer,
    parse_countries,
    parse_remote_text,
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
UNDO_KEY = "profile.undo"
REMATCH_WINDOW = timedelta(days=7)
MAX_ROLES = 10
MAX_SKILLS = 40
SHOWN_SKILLS = 25
_OK = frozenset({"ok", "okay", "yes", "y", "keep", "same"})
_OK_EMOJI = frozenset({"👍", "👌", "✅", "✔️", "🆗", "👍🏻", "👍🏽", "👍🏾"})


class Step(StrEnum):
    """Questions, in order. Every step except RESUME and CONFIRM is also a profile ``Field``."""

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
WHY: dict[Field, str] = {
    Field.ROLES: "I search LinkedIn, Indeed and other job boards with these titles.",
    Field.HOME: "It decides which remote jobs you can take and where you'd need a visa.",
    Field.COUNTRIES: "On-site and hybrid jobs are only shown in these countries (and your home country).",
    Field.VISA_FREE: "In other countries, jobs that say they don't sponsor visas are hidden for you.",
    Field.REMOTE: "It decides whether you see remote, hybrid or office jobs.",
    Field.YEARS: "Jobs asking for far more experience than you have are skipped; it also sets your level.",
    Field.SKILLS: "Each job is scored by how many of its must-have skills you have.",
    Field.SALARY: "Jobs paying less than this (in the same currency) score lower. It's optional.",
}
START_TEXT = (
    "Let's set up your profile (about 2 minutes). Send /cancel to stop.\n\n"
    "📄 First, send me your <b>resume</b> as a PDF or Word (.docx) file."
)
SAVED_TEXT = (
    "✅ Saved. I'm matching {count} recent job(s) against your profile, and every new one from now on.\n"
    "Strong matches come right away, the rest in your daily digest. /profile shows your profile.\n"
    "You can change anything any time, e.g. <i>add UAE to my countries</i>."
)
EDIT_HINT = "Reply <b>ok</b> to use it, or e.g. <i>ok, remove X, add Y</i>, or type your own."
UNKNOWN_PLACES = "I don't know {places}. Use country names (or 'none')."
_NO_RESUME = re.compile(
    r"^(?:no(?:\s+resume|\s+cv)?|later|not\s+now|don'?t\s+have(?:\s+(?:one|it|a\s+(?:resume|cv)))?|"
    r"i\s+don'?t\s+have(?:\s+(?:one|it|a\s+(?:resume|cv)))?|without(?:\s+(?:resume|cv))?|nope|next)[\s.!]*$",
    re.IGNORECASE,
)
_SAVE_WORDS = re.compile(
    r"^(?:save|yes|y|ok(?:ay)?|done|confirm|correct|looks\s+good|all\s+good|perfect|go|go\s+ahead)[\s.!]*$",
    re.IGNORECASE,
)
_RESTART_WORDS = re.compile(r"^(?:start\s+over|restart|redo|again|from\s+start|reset)[\s.!]*$", re.IGNORECASE)
VISA_ANYWHERE = (
    "Hardly anyone can work everywhere without a visa 🙂 If you need a visa outside {home}, reply "
    "<b>none</b>. If you hold another passport or permit, name those countries "
    "(e.g. Europe for an EU passport)."
)
_LIST_FIELDS = {Field.ROLES, Field.SKILLS}
_COUNTRY_FIELDS = {Field.COUNTRIES, Field.VISA_FREE}


def _names(codes: list[str] | frozenset[str]) -> str:
    """Country names, alphabetically ("Anywhere" for every country)."""
    if len(codes) >= len(ALL_COUNTRIES):
        return "Anywhere"
    return ", ".join(sorted(country_name(c) for c in codes)) or "none"


def _salary_text(draft: dict[str, Any]) -> str:
    salary = draft.get("salary")
    return f"{salary['currency']} {salary['minimum']:,.0f} / {salary['period']}" if salary else "not set"


_UNDERSTOOD: dict[Field, tuple[str, Callable[[dict[str, Any]], str]]] = {
    Field.ROLES: ("Roles", lambda d: ", ".join(d.get("roles") or []) or "none"),
    Field.HOME: ("Lives in", lambda d: country_name(d["home"])),
    Field.COUNTRIES: ("Other countries", lambda d: _names(d.get("countries") or [])),
    Field.VISA_FREE: ("No visa needed in (besides home)", lambda d: _names(d.get("visa_free") or [])),
    Field.REMOTE: ("Remote", lambda d: _REMOTE_LABELS[RemotePreference(d["remote"])]),
    Field.YEARS: ("Experience", lambda d: f"{d['years']:g} years"),
    Field.SKILLS: ("Skills", lambda d: ", ".join(d.get("skills") or []) or "none"),
    Field.SALARY: ("Minimum salary", _salary_text),
}


def understood(field: Field, draft: dict[str, Any]) -> str:
    """How Sarthi read an answer, e.g. "✓ Roles: <b>Backend Engineer</b>"."""
    label, render = _UNDERSTOOD[field]
    return f"✓ {label}: <b>{escape(render(draft))}</b>"


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


def draft_from(profile: Profile) -> dict[str, Any]:
    """A saved profile as setup answers (so it can be edited the same way)."""
    p = profile.preferences
    return {
        "roles": list(p.roles),
        "home": p.home_country,
        "countries": sorted(p.countries),
        "visa_free": sorted(p.authorized_countries),
        "remote": p.remote.value,
        "years": p.years_experience,
        "skills": [*profile.skills, *p.keywords],
        "salary": p.min_salary.model_dump(mode="json") if p.min_salary else None,
        "resume_text": profile.resume_text,
    }


def profile_from(draft: dict[str, Any]) -> Profile:
    """Setup answers as a profile. Raises ``ValidationError`` or ``KeyError`` when something is missing."""
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


def _apply_home(draft: dict[str, Any], suggest: dict[str, Any], text: str) -> str | None:
    suggested = suggest.get("home") if text.lower() in _OK else None
    found = parse_countries(text).codes
    home = suggested or (next(iter(found)) if len(found) == 1 else first_country(text))
    if not home:
        return "Please name one country, e.g. India or United Kingdom."
    draft["home"] = home
    return None


def _apply_remote(draft: dict[str, Any], suggest: dict[str, Any], text: str) -> str | None:
    preference = parse_remote_text(text)
    if preference is None:
        return "Please tap one of the buttons."
    draft["remote"] = preference.value
    return None


def _apply_years(draft: dict[str, Any], suggest: dict[str, Any], text: str) -> str | None:
    years = suggest.get("years") if text.lower() in _OK else parse_years(text)
    if years is None:
        return "Please reply with a number of years, e.g. 3 or 0 for a fresher."
    draft["years"] = years
    return None


def _apply_salary(draft: dict[str, Any], text: str) -> str | None:
    if is_none(text) or is_salary_skip(text):
        draft["salary"] = None
        return None
    salary = find_salary(text, draft.get("home") or "")
    if salary is None:
        return "I didn't get that. Try e.g. 12 LPA, 80k GBP, USD 120000 — or skip."
    draft["salary"] = salary.model_dump(mode="json")
    return None


class SetupFlow:
    """Runs the setup conversation and applies profile changes for the paired user."""

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

    @property
    def step(self) -> Step | None:
        """The question being asked, or None outside setup."""
        state = self._state()
        return Step(state["step"]) if state else None

    @property
    def asking(self) -> Field | None:
        """The profile field of the question being asked (None for the resume and summary steps)."""
        step = self.step
        return Field(step.value) if step is not None and step.value in Field.__members__.values() else None

    def fits_current_question(self, text: str) -> bool:
        """True when ``text`` is a clear answer to the (non-list) question being asked.

        "anywhere without visa" mentions visas, but while countries are being asked it answers that question;
        it must not be taken as a change to the visa answer.
        """
        field, text = self.asking, normalize_answer(text)
        if field is None or field in _LIST_FIELDS:
            return False
        if field in _COUNTRY_FIELDS:
            parsed = parse_countries(text)
            return not parsed.unknown and (bool(parsed.codes) or parsed.anywhere or parsed.nothing)
        checks: dict[Field, Callable[[str], bool]] = {
            Field.HOME: lambda t: len(parse_countries(t).codes) == 1 or first_country(t) is not None,
            Field.REMOTE: lambda t: parse_remote_text(t) is not None,
            Field.YEARS: lambda t: parse_years(t) is not None,
            Field.SALARY: lambda t: is_salary_skip(t) or find_salary(t, "IN") is not None,
        }
        return checks[field](text)

    def question_text(self) -> str:
        """The current question as plain words, for the AI that routes messages."""
        step = self.step
        return QUESTIONS.get(step, "") if step is not None else ""

    # ---- entry points -----------------------------------------------------------------------------------

    async def start(self, chat_id: int) -> None:
        """Begin (or restart) setup; current profile values become the suggestions."""
        current = self._load(self.profile_file)
        suggest: dict[str, Any] = {}
        draft: dict[str, Any] = {}
        if current is not None:
            saved = draft_from(current)
            suggest = {key: saved[key] for key in ("roles", "skills", "years", "home")}
            draft["resume_text"] = current.resume_text
        self._save_state({"step": Step.RESUME, "draft": draft, "suggest": suggest, "answered": []})
        keep = " Or reply <b>skip</b> to keep the one you sent before." if draft.get("resume_text") else ""
        await self.api.send_message(chat_id, START_TEXT + (keep or " Or reply <b>skip</b>."))

    async def show(self, chat_id: int) -> None:
        """Send the saved profile, or how to create one."""
        profile = self._load(self.profile_file)
        if profile is None:
            await self.api.send_message(chat_id, "You have no profile yet. Send /setup to create one.")
            return
        await self.api.send_message(chat_id, summary(profile) + "\n\nChange anything by just telling me.")

    async def cancel(self, chat_id: int) -> None:
        """Stop setup without saving."""
        self._save_state(None)
        await self.api.send_message(chat_id, "Setup cancelled. Nothing was changed. /setup to start again.")

    async def ask_again(self, chat_id: int) -> None:
        """Repeat the current question (after small talk or a side question)."""
        state = self._state()
        if state is not None:
            await self._ask(chat_id, state)

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
        text = normalize_answer(text)
        if text in _OK_EMOJI:
            text = "ok"
        if step is Step.RESUME:
            if is_none(text) or _NO_RESUME.match(text):
                await self._advance(chat_id, state)
            else:
                await self.api.send_message(
                    chat_id, "Please send the resume as a file (📎 → File), or reply skip."
                )
            return
        if step is Step.CONFIRM:
            if _SAVE_WORDS.match(text):
                await self._finish(chat_id, state)
            elif _RESTART_WORDS.match(text):
                await self.start(chat_id)
            else:
                await self.api.send_message(chat_id, "Tap ✅ Save (or reply <b>save</b>), or 🔄 Start over.")
            return
        field = Field(step.value)
        error = await self._apply(chat_id, state, field, text)
        if error:
            await self.api.send_message(chat_id, error)
            return
        state["answered"] = sorted({*state.get("answered", []), field.value})
        change = buttons([("✏️ Change", f"setup:back:{field.value}")])
        await self.api.send_message(chat_id, understood(field, state["draft"]), change)
        await self._advance(chat_id, state)

    async def correct(self, chat_id: int, field: Field, text: str) -> None:
        """Change one answer from a free message: during setup the draft, otherwise the saved profile."""
        text = normalize_answer(text)
        state = self._state()
        if state is not None:
            error = await self._apply(chat_id, state, field, text, use_suggestion=False)
            if error:
                await self.api.send_message(chat_id, error)
                return
            state["answered"] = sorted({*state.get("answered", []), field.value})
            self._save_state(state)
            await self.api.send_message(chat_id, understood(field, state["draft"]) + " (updated)")
            await self._ask(chat_id, state)
            return
        profile = self._load(self.profile_file)
        if profile is None:
            await self.api.send_message(chat_id, "You have no profile yet. Send /setup to create one.")
            return
        edit_state: dict[str, Any] = {"draft": draft_from(profile), "suggest": {}}
        error = await self._apply(chat_id, edit_state, field, text, use_suggestion=False)
        if error:
            await self.api.send_message(chat_id, error)
            return
        try:
            updated = profile_from(edit_state["draft"])
        except (ValidationError, KeyError):
            await self.api.send_message(
                chat_id, "That would leave your profile incomplete, so I didn't change it."
            )
            return
        self.chats.set_state(UNDO_KEY, profile.model_dump_json())
        save_profile(self.profile_file, updated)
        self._rematch()
        undo = buttons([("↩️ Undo", "profile:undo")])
        await self.api.send_message(chat_id, understood(field, edit_state["draft"]) + " (saved)", undo)

    async def undo(self, chat_id: int) -> None:
        """Put back the profile as it was before the last change made by message."""
        raw = self.chats.get_state(UNDO_KEY)
        if not raw:
            await self.api.send_message(chat_id, "Nothing to undo.")
            return
        save_profile(self.profile_file, Profile.model_validate_json(raw))
        self.chats.set_state(UNDO_KEY, None)
        self._rematch()
        await self.api.send_message(chat_id, "↩️ Undone. /profile shows your profile.")

    async def on_button(self, chat_id: int, data: str) -> None:
        """Setup buttons: ``setup:remote:<v>``, ``setup:save``, ``setup:restart``, ``setup:back:<step>``."""
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

    # ---- reading answers (each returns an error message, or None when the draft was updated) ----------

    async def _apply(
        self, chat_id: int, state: dict[str, Any], field: Field, text: str, *, use_suggestion: bool = True
    ) -> str | None:
        if field in _LIST_FIELDS:
            return await self._apply_list(chat_id, state, field, text)
        if field in _COUNTRY_FIELDS:
            return await self._apply_countries(chat_id, state["draft"], field, text)
        apply_one = {Field.HOME: _apply_home, Field.REMOTE: _apply_remote, Field.YEARS: _apply_years}
        suggest = state["suggest"] if use_suggestion else {}
        if field is Field.SALARY:
            return _apply_salary(state["draft"], text)
        return apply_one[field](state["draft"], suggest, text)

    async def _edited_list(
        self, chat_id: int, question: str, current: list[str], text: str
    ) -> list[str] | str:
        """The list after an answer: rules for simple answers, the AI checked by rules for free-form ones."""
        if not is_simple_answer(text):
            if self.ai.available:
                await self.api.send_message(chat_id, "🤔 Understanding your answer…")
            items = await interpret_list(self.ai, question, current, text)
            if items is not None:
                items = reconcile(current, items, text)  # rules catch what a small model drops or misses
                known = {item.lower() for item in current}
                added = [item for item in items if item.lower() not in known]
                if not any(looks_like_instruction(item) or clean_item(item) is None for item in added):
                    return items
        edit = edit_list(text, current)
        return edit.error if edit.error else list(edit.items)

    async def _apply_list(self, chat_id: int, state: dict[str, Any], field: Field, text: str) -> str | None:
        key = field.value
        current = list(state["draft"].get(key) or state["suggest"].get(key) or [])
        result = await self._edited_list(chat_id, QUESTIONS[Step(key)], current, text)
        if isinstance(result, str):
            return result
        if field is Field.ROLES and not result:
            return "Please name at least one job title, e.g. Backend Engineer, Data Analyst."
        state["draft"][key] = result[: MAX_ROLES if field is Field.ROLES else MAX_SKILLS]
        return None

    async def _apply_countries(
        self, chat_id: int, draft: dict[str, Any], field: Field, text: str
    ) -> str | None:
        """Countries from a plain list ("UK, UAE") or an edit of the current ones ("add UAE, remove UK")."""
        current = [country_name(code) for code in draft.get(field.value) or []]
        names = text
        direct = parse_countries(text)
        complete = not direct.unknown and (bool(direct.codes) or direct.anywhere or direct.nothing)
        if not complete and not is_simple_answer(text):  # an edit of the current list ("add UAE, remove UK")
            result = await self._edited_list(chat_id, QUESTIONS[Step(field.value)], current, text)
            if isinstance(result, str):
                return result
            names = ", ".join(result) or "none"
        parsed = parse_countries(names)
        if parsed.anywhere and field is Field.VISA_FREE:
            return VISA_ANYWHERE.format(home=escape(country_name(draft.get("home", ""))))
        if parsed.unknown:
            return UNKNOWN_PLACES.format(places=escape(", ".join(parsed.unknown)))
        draft[field.value] = sorted(parsed.codes)
        return None

    # ---- flow ---------------------------------------------------------------------------------------------

    async def _go_back(self, chat_id: int, state: dict[str, Any], step_name: str) -> None:
        """Ask an earlier question again; afterwards the flow returns to where the user was."""
        if step_name not in Step.__members__.values() or step_name == Step.RESUME:
            return
        step = Step(step_name)
        if _ORDER.index(step) > _ORDER.index(Step(state["step"])):
            return
        current = state["step"]
        state["step"] = step
        state["back_to"] = state.get("back_to") or current
        self._save_state(state)
        await self._ask(chat_id, state)

    async def _advance(self, chat_id: int, state: dict[str, Any]) -> None:
        if back_to := state.pop("back_to", None):  # a corrected answer returns to where the user was
            state["step"] = back_to
        else:
            answered = set(state.get("answered", []))
            index = _ORDER.index(Step(state["step"])) + 1
            while _ORDER[index] is not Step.CONFIRM and _ORDER[index].value in answered:
                index += 1  # already answered in a correction: don't ask twice
            state["step"] = _ORDER[index]
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
        if step is Step.RESUME:
            await self.api.send_message(
                chat_id, "📄 Please send your resume as a file, or reply <b>skip</b>."
            )
            return
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
            profile = profile_from(state["draft"])
        except (ValidationError, KeyError) as error:
            log.warning("setup produced an invalid profile: %s", error)
            await self.api.send_message(chat_id, "Something didn't add up. Let's start again.")
            await self.start(chat_id)
            return
        choices = buttons([("✅ Save", "setup:save"), ("🔄 Start over", "setup:restart")])
        await self.api.send_message(chat_id, summary(profile), choices)

    def _rematch(self) -> int:
        recent = self.jobs.ids_seen_since(self._clock() - REMATCH_WINDOW)
        for job_id in recent:
            self.queue.enqueue(names.MATCH, job_id, priority=Priority.BACKGROUND)
        return len(recent)

    async def _finish(self, chat_id: int, state: dict[str, Any]) -> None:
        save_profile(self.profile_file, profile_from(state["draft"]))
        self._save_state(None)
        await self.api.send_message(chat_id, SAVED_TEXT.format(count=self._rematch()))
