"""Job identity: one canonical URL per link, and company|role keys that join reposts across boards.

The same job reaches Sarthi from many places (company board, LinkedIn, Naukri, Freehire, a Telegram post).
``job_id_for_url`` gives each canonical link a stable id; ``role_key`` joins one role posted on several
boards or for several cities into one job.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = frozenset(
    {"ref", "src", "source", "fbclid", "gclid", "trk", "si", "igshid", "mc_cid", "mc_eid",
     "trackingid", "refid", "lipi", "ref_src", "_hsenc", "_hsmi",
     "gh_src", "lever-source", "lever-origin"}
)  # fmt: skip
_DEFAULT_PORTS = {"http": 80, "https": 443}

_NOISE_WORDS = frozenset(
    {"pvt", "private", "ltd", "limited", "llp", "inc", "llc", "corp", "co", "hiring",
     "is", "for", "the", "we", "are",
     "gmbh", "plc", "pte", "pty", "bv"}
)  # fmt: skip
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalise_url(url: str) -> str | None:
    """Canonical form of a link without network access; None if it is not a usable http(s) URL.

    Lower-cases scheme and host, drops ``www.``, default ports, fragments, trailing slashes and
    tracking parameters, and sorts the remaining query parameters.
    """
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    if scheme not in _DEFAULT_PORTS or "." not in host:
        return None
    if port and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not (key.lower().startswith("utm_") or key.lower() in _TRACKING_EXACT)
    )
    return urlunsplit((scheme, host, parts.path.rstrip("/"), urlencode(query), ""))


def job_id_for_url(canonical_url: str) -> str:
    """Stable 40-character id for a canonical job URL (not a security hash)."""
    return hashlib.sha1(canonical_url.encode("utf-8"), usedforsecurity=False).hexdigest()


def norm(text: str | None) -> str:
    """Lower-case, drop punctuation and filler words like 'pvt', 'ltd', 'hiring'."""
    words = _NON_WORD.sub(" ", (text or "").lower()).split()
    return " ".join(word for word in words if word not in _NOISE_WORDS)


def fingerprint(company: str | None, role: str | None, location: str | None) -> str | None:
    """``norm(company)|norm(role)|norm(location)``; None unless both company and role are known."""
    key = role_key(company, role)
    return f"{key}|{norm(location)}" if key else None


def role_key(company: str | None, role: str | None) -> str | None:
    """``norm(company)|norm(role)`` with the location left out: one role posted for ten cities is one job.

    None unless both company and role are known.
    """
    company_key, role_text = norm(company), norm(role)
    if not company_key or not role_text:
        return None
    return f"{company_key}|{role_text}"
