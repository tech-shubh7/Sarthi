"""Outbound-request guard: only talk to public internet hosts (blocks SSRF through job links).

The rule lives in ``sarthi.jobs.net`` (shared with the apply step); this keeps the sources' import path.
"""

from __future__ import annotations

from sarthi.jobs.net import is_public_host, is_public_url, resolve

__all__ = ["is_public_host", "is_public_url", "resolve"]
