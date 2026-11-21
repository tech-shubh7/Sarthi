# Adapted from JobRadar (MIT), src/jobradar/net.py @ fdd739b.
# Copyright (c) ThePlator. Modifications (c) Sarthi contributors.
# Changes: the name lookup is a module function so tests can replace it.
"""Outbound-request guard: only talk to public internet hosts (blocks SSRF through job links)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

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
