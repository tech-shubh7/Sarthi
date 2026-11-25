"""The employer's real application questions, read from the hiring system's public data, not a browser.

Greenhouse and Ashby have public APIs; Lever's public apply page is plain HTML. Knowing the questions before the
browser opens means unknown ones can be asked on Telegram first, so the application never stops half-filled.
Any failure returns None and the browser reads the form itself.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from sarthi import __version__
from sarthi.jobs.forms import FieldType, FormField

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
MAX_BYTES = 3_000_000
USER_AGENT = f"Sarthi/{__version__} (self-hosted job search)"


@dataclass(frozen=True, slots=True)
class Posting:
    """Which hiring system a job's form is on, the company's board name and the posting id."""

    system: str  # "greenhouse", "lever" or "ashby"
    board: str
    posting: str


_GREENHOUSE_HOST = re.compile(r"^(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io$")
_LEVER_HOST = re.compile(r"^jobs(?:\.eu)?\.lever\.co$")
_ASHBY_HOST = "jobs.ashbyhq.com"
_SLUG = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def posting_for(*urls: str) -> Posting | None:
    """The form's hiring system, board and posting, from the first link that names them."""
    for url in urls:
        if not url:
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        parts = [p for p in parsed.path.split("/") if p]
        if _GREENHOUSE_HOST.match(host) and len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit():  # noqa: PLR2004
            found = Posting("greenhouse", parts[0], parts[2])
        elif _GREENHOUSE_HOST.match(host) and "token" in parse_qs(parsed.query) and len(parts) >= 1:
            found = Posting("greenhouse", parts[0], parse_qs(parsed.query)["token"][0])
        elif _LEVER_HOST.match(host) and len(parts) >= 2:  # noqa: PLR2004
            found = Posting("lever", parts[0], parts[1])
        elif host == _ASHBY_HOST and len(parts) >= 2:  # noqa: PLR2004
            found = Posting("ashby", parts[0], parts[1])
        else:
            continue
        if _SLUG.match(found.board) and _SLUG.match(found.posting):
            return found
    return None


# ---- Greenhouse ---------------------------------------------------------------------------------------------

_GREENHOUSE_TYPES: dict[str, FieldType] = {
    "input_text": "text", "textarea": "textarea", "input_file": "file",
    "multi_value_single_select": "select", "multi_value_multi_select": "checkbox",
}  # fmt: skip


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def greenhouse_fields(data: Any) -> list[FormField]:
    """Fields from Greenhouse's ``/jobs/<id>?questions=true`` answer."""
    out: list[FormField] = []
    if not isinstance(data, dict):
        return out
    for question in [*(data.get("questions") or []), *(data.get("location_questions") or [])]:
        if not isinstance(question, dict):
            continue
        fields = [f for f in question.get("fields") or [] if isinstance(f, dict)]
        chosen = next((f for f in fields if f.get("type") == "input_file"), fields[0] if fields else None)
        label = _text(question.get("label"))
        if not chosen or chosen.get("type") == "input_hidden" or not label:
            continue
        options = tuple(_text(v.get("label")) for v in chosen.get("values") or [] if isinstance(v, dict))
        out.append(
            FormField(
                key=_text(chosen.get("name")),
                label=label,
                type=_GREENHOUSE_TYPES.get(str(chosen.get("type")), "unknown"),
                required=bool(question.get("required")),
                options=tuple(o for o in options if o),
            )
        )
    return out


# ---- Ashby ------------------------------------------------------------------------------------------------

ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    applicationForm { sections { fieldEntries { ... on FormFieldEntry { field isRequired } } } }
  }
}"""
_ASHBY_TYPES: dict[str, FieldType] = {
    "String": "text", "Email": "email", "Phone": "tel", "LongText": "textarea", "File": "file",
    "ValueSelect": "select", "MultiValueSelect": "checkbox", "Number": "number", "Date": "date", "Boolean": "select",
}  # fmt: skip


def ashby_fields(data: Any) -> list[FormField]:
    """Fields from Ashby's ``ApiJobPosting`` GraphQL answer."""
    out: list[FormField] = []
    try:
        sections = data["data"]["jobPosting"]["applicationForm"]["sections"] or []
    except (KeyError, TypeError):
        return out
    for section in sections:
        for entry in (section or {}).get("fieldEntries") or []:
            f = (entry or {}).get("field")
            if not isinstance(f, dict) or f.get("isDeactivated") or not _text(f.get("title")):
                continue
            if f.get("type") == "Boolean":
                options: tuple[str, ...] = ("Yes", "No")
            else:
                values = f.get("selectableValues") or []
                options = tuple(
                    _text(v.get("label")) for v in values if isinstance(v, dict) and not v.get("isArchived")
                )
            out.append(
                FormField(
                    key=_text(f.get("path")),
                    label=_text(f.get("title")),
                    type=_ASHBY_TYPES.get(str(f.get("type")), "unknown"),
                    required=bool(entry.get("isRequired")),
                    options=tuple(o for o in options if o),
                )
            )
    return out


# ---- Lever (the public apply page) ------------------------------------------------------------------------

_NOT_ASKED = re.compile(r"^(eeo|surveysResponses|consent)\[")  # profile-level surveys and consent


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\s{name}\s*=\s*"([^"]*)"', tag, re.I)
    return m.group(1) if m else None


def _plain(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return re.sub(r"\s+", " ", re.sub(r"[*✱]", "", text)).strip()


def lever_fields(page: str) -> list[FormField]:
    """Fields from Lever's apply page. A question without a readable label means an unknown layout: nothing."""
    form = re.search(r'<form\b[^>]*\bid="application-form"[\s\S]*?</form>', page, re.I)
    if not form:
        return []
    out: list[FormField] = []
    for chunk in re.split(
        r'(?=<(?:li|div)\b[^>]*\bclass="(?:application-question|section)[\s"])', form.group()
    ):
        controls = [
            m.group()
            for m in re.finditer(r"<select\b[^>]*>[\s\S]*?</select>|<(?:input|textarea)\b[^>]*>", chunk, re.I)
            if (name := _attr(m.group(), "name"))
            and not _NOT_ASKED.match(name)
            and (_attr(m.group(), "type") or "").lower() != "hidden"
        ]
        if not controls:
            continue
        first = controls[0]
        key = _attr(first, "name") or ""
        label_html = re.search(
            r'\bclass="application-label[^"]*">([\s\S]*?)<div class="application-field', chunk
        )
        label = _plain(label_html.group(1)) if label_html else ""
        label = label or ("Additional information" if key == "comments" else "")
        if not label:
            return []
        tag = re.match(r"^<(\w+)", first).group(1).lower()  # type: ignore[union-attr]
        kind = (_attr(first, "type") or "text").lower()
        group = [c for c in controls if _attr(c, "name") == key]
        if tag == "select":
            options = tuple(_plain(o) for o in re.findall(r"<option\b[^>]*>([\s\S]*?)</option>", first, re.I))
        elif kind in ("radio", "checkbox"):
            options = tuple(
                _plain(_attr(c, "value") or "") for c in group if (_attr(c, "type") or "").lower() == kind
            )
        else:
            options = ()
        field_type: FieldType
        if tag == "textarea":
            field_type = "textarea"
        elif tag == "select" or kind == "radio":
            field_type = "select"
        elif kind in ("checkbox", "file", "email", "tel", "url", "number", "date"):
            field_type = kind  # type: ignore[assignment]
        else:
            field_type = "text"
        required = bool(label_html and 'class="required"' in label_html.group(1)) or bool(
            re.search(r"\srequired(?=[\s=>/])", first)
        )
        out.append(FormField(key, label, field_type, required, tuple(o for o in options if o)))
    return out


# ---- reading ----------------------------------------------------------------------------------------------


async def _get(http: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response | None:
    try:
        response = await (http.post(url, **kwargs) if "json" in kwargs else http.get(url, **kwargs))
    except httpx.HTTPError as error:
        log.info("form questions not read (%s): %s", url, type(error).__name__)
        return None
    if response.status_code != httpx.codes.OK or len(response.content) > MAX_BYTES:
        log.info("form questions not read (%s): HTTP %s", url, response.status_code)
        return None
    return response


async def read_questions(
    posting: Posting, transport: httpx.AsyncBaseTransport | None = None
) -> list[FormField] | None:
    """The form's questions in form order, or None when they could not be read (never raises)."""
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers, transport=transport) as http:
        try:
            if posting.system == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{posting.board}/jobs/{posting.posting}"
                response = await _get(http, url, params={"questions": "true"})
                fields = greenhouse_fields(response.json()) if response else None
            elif posting.system == "ashby":
                body = {
                    "operationName": "ApiJobPosting",
                    "variables": {
                        "organizationHostedJobsPageName": posting.board,
                        "jobPostingId": posting.posting,
                    },
                    "query": ASHBY_QUERY,
                }
                response = await _get(
                    http, "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", json=body
                )
                fields = ashby_fields(response.json()) if response else None
            elif posting.system == "lever":
                url = f"https://jobs.lever.co/{posting.board}/{posting.posting}/apply"
                response = await _get(http, url)
                fields = lever_fields(response.text) if response else None
            else:
                return None
        except ValueError:  # an answer that is not JSON
            return None
    return fields or None
