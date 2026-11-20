"""Reading and writing the profile file in the user's data folder.

The file holds the user's resume, so it is written atomically (a crash never leaves half a file) and is
readable by its owner only.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from sarthi.profile.models import Profile

PROFILE_FILE = "profile.json"
_OWNER_ONLY = 0o600


def profile_path(data_dir: Path) -> Path:
    """Where the profile lives inside ``data_dir``."""
    return data_dir / PROFILE_FILE


def load_profile(path: Path) -> Profile | None:
    """The saved profile, or None when there is none yet. Raises ``ValidationError`` for a damaged file."""
    if not path.is_file():
        return None
    return Profile.model_validate_json(path.read_text(encoding="utf-8"))


def save_profile(path: Path, profile: Profile) -> None:
    """Write ``profile`` atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".profile-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, _OWNER_ONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(profile.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
