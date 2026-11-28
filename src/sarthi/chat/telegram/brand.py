"""Give the user's own Telegram bot Sarthi's logo and description.

A bot made by one-tap setup (or by hand in @BotFather) starts with a blank picture and no description. Once
per bot, Sarthi fills in whichever of the two is still empty. Anything the user set themselves is kept, and
the shared relay bot (no bot id in its getMe) is never touched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from sarthi.queue.tasks import PermanentError, RetryableError

log = logging.getLogger(__name__)

BRANDED_KEY = "telegram.branded"  # the id of the bot already given the logo and description
AVATAR = Path(__file__).with_name("sarthi-avatar.jpg")
DESCRIPTION = (
    "Sarthi is your AI job assistant. It finds the jobs that fit your resume and preferences, sends you the "
    "strong matches, writes a cover letter for each job you pick, and fills in the application.\n\n"
    "This bot is yours alone and talks only to your own Sarthi."
)
SHORT_DESCRIPTION = "Your AI job assistant: matched jobs, cover letters and applications."


class _Api(Protocol):
    async def me(self) -> dict[str, object]: ...
    async def has_profile_photo(self, user_id: int) -> bool: ...
    async def set_profile_photo(self, jpeg: bytes) -> None: ...
    async def description(self) -> str: ...
    async def set_description(self, description: str, short: str) -> None: ...


class _State(Protocol):
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str | None) -> None: ...


async def brand_bot(api: _Api, state: _State) -> bool:
    """Set the logo and description where still empty; True when this bot is done (now or before)."""
    try:
        bot_id = (await api.me()).get("id")
        if not isinstance(bot_id, int):
            return False  # the shared relay bot: its look is the relay's
        if state.get_state(BRANDED_KEY) == str(bot_id):
            return True
        if not await api.has_profile_photo(bot_id):
            await api.set_profile_photo(AVATAR.read_bytes())
        if not (await api.description()).strip():
            await api.set_description(DESCRIPTION, SHORT_DESCRIPTION)
    except (RetryableError, PermanentError, OSError) as error:
        log.warning("telegram: could not set the bot's picture or description: %s", error)
        return False  # tried again at the next start
    state.set_state(BRANDED_KEY, str(bot_id))
    log.info("telegram: gave your bot Sarthi's logo and description")
    return True
