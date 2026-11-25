"""The schema.org JobPosting many job pages embed for search engines (``application/ld+json``).

Written by the employer or job board itself, it carries the full description even on pages that build their
text with JavaScript (Workday and others), so it is read before the page's visible text.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

_LD_JSON = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
MAX_DESCRIPTION = 20_000
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr)\b[^>]*>", re.IGNORECASE)


def _items(node: Any) -> list[Any]:
    """Every object in a JSON-LD document, including those inside lists and @graph."""
    if isinstance(node, list):
        return [item for child in node for item in _items(child)]
    if isinstance(node, dict):
        return [node, *_items(node.get("@graph", []))]
    return []


def _is_job_posting(item: dict[str, Any]) -> bool:
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "JobPosting" in (str(k) for k in kinds)


def _load(block: str) -> Any:
    try:
        return json.loads(block.strip(), strict=False)  # raw newlines inside strings happen
    except ValueError:
        return None


def find_job_posting(html: str) -> dict[str, Any] | None:
    """The first JobPosting object in the page, or None. Long descriptions are trimmed."""
    for block in _LD_JSON.findall(html):
        data = _load(block) or _load(html_lib.unescape(block))  # some sites escape the block
        for item in _items(data):
            if _is_job_posting(item):
                if isinstance(item.get("description"), str):
                    item["description"] = item["description"][:MAX_DESCRIPTION]
                return dict(item)
    return None


def description_text(posting: dict[str, Any]) -> str | None:
    """The JobPosting description as plain text (HTML removed), or None."""
    raw = posting.get("description")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _TAG.sub(" ", _BLOCK.sub("\n", html_lib.unescape(raw)))
    lines = (" ".join(line.split()) for line in html_lib.unescape(text).splitlines())
    return "\n".join(line for line in lines if line) or None
