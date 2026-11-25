"""Outbound-request guard for the pipeline steps: only public internet hosts (blocks SSRF via job links).

The same rule as ``sarthi.sources.net`` (the fetcher's copy), here in the shared layer so the apply step can
check a job board's "Apply on company site" link before its browser opens it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal")


async def resolve(host: str) -> set[str]:
    """Every address ``host`` resolves to (replaced in tests)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return {str(info[4][0]) for info in infos}


async def is_public_host(host: str) -> bool:
    """True only if every address the host resolves to is a public (global) IP."""
    host = host.strip("[]").lower()
    if not host or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass  # a name, not a literal IP
    try:
        addresses = await resolve(host)
    except (OSError, UnicodeError):
        return False
    return bool(addresses) and all(ipaddress.ip_address(a.split("%", 1)[0]).is_global for a in addresses)


async def is_public_url(url: str) -> bool:
    """An http(s) link to a public host, with no user name or password in it."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return False
    return await is_public_host(host)
