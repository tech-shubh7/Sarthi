"""Reading and writing the profile file in the user's data folder.

The file holds the user's resume, so it is written atomically (a crash never leaves half a file) and is
readable by its owner only.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import ValidationError

from sarthi.profile.models import Profile

log = logging.getLogger(__name__)

PROFILE_FILE = "profile.json"
DAMAGED_FILE = "profile.damaged.json"
_OWNER_ONLY = 0o600
_WARNED: dict[Path, int] = {}  # damaged profile files already reported, by their modification time


def profile_path(data_dir: Path) -> Path:
    """Where the profile lives inside ``data_dir``."""
    return data_dir / PROFILE_FILE


def load_profile(path: Path) -> Profile | None:
    """The saved profile, or None when there is none yet (or the file is damaged).

    A damaged file (a typo made by hand, a disk that filled up) stopped Sarthi from starting at all
    (2026-11-28). It is now treated as "no profile yet": a copy is kept beside it as ``profile.damaged.json``
    and the dashboard asks for the profile again, as on the first day.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return Profile.model_validate_json(text)
    except (ValidationError, UnicodeDecodeError, OSError) as error:
        _keep_damaged(path, error)
        return None


def _keep_damaged(path: Path, error: Exception) -> None:
    """Copy the damaged profile aside (once per change) and say so."""
    copy = path.with_name(DAMAGED_FILE)
    try:
        stamp = path.stat().st_mtime_ns
        if _WARNED.get(path) == stamp:
            return
        _WARNED[path] = stamp
        shutil.copyfile(path, copy)
        copy.chmod(_OWNER_ONLY)
    except OSError:
        pass
    log.error(
        "Your profile file (%s) is damaged, so Sarthi runs as if it were not set up yet (%s). A copy is kept "
        "in %s. Fill in Settings > Profile on the dashboard again, or fix the file and restart Sarthi.",
        path,
        type(error).__name__,
        copy.name,
    )


def save_profile(path: Path, profile: Profile) -> None:
    """Write ``profile`` atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".profile-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):  # Windows has none: its user folder is already private to the user
            os.fchmod(fd, _OWNER_ONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(profile.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
