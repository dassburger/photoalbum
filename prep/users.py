"""users.yml load/save/add/update/remove, bcrypt password hashing."""

from __future__ import annotations

from pathlib import Path

import bcrypt
from ruamel.yaml import YAML

from prep.atomic import atomic_write

_yaml = YAML()
_yaml.preserve_quotes = True


class UserError(Exception):
    """Raised for user-management errors (duplicate/unknown username)."""


def load_users(path: str | Path):
    """Load users.yml via ruamel.yaml round-trip mode.

    Returns an empty CommentedMap-like mapping if the file doesn't exist yet,
    so callers can add the first user to a brand-new users.yml.
    """
    path = Path(path)
    if not path.exists():
        return _yaml.load("{}\n") or _yaml.map()
    with path.open("r") as f:
        data = _yaml.load(f)
    if data is None:
        data = _yaml.load("{}\n")
    return data


def save_users(path: str | Path, data) -> None:
    """Save users.yml via ruamel.yaml round-trip mode, preserving formatting.

    Written atomically (temp file + rename, see prep/atomic.py) so a crash
    mid-write never leaves users.yml truncated/corrupt -- same crash-safety
    rule the plan requires for anything touching persisted output (see
    Quality review finding #2).

    Explicitly overrides atomic_write's 0o644 default to 0o600: unlike
    every other generated/persisted file (thumbnails, HTML, the Caddyfile,
    publish.yaml), users.yml holds bcrypt password hashes and is read only
    by the prep container itself -- it should never be group/world-
    readable (see iteration-2 review finding #1).
    """
    path = Path(path)

    def _save(tmp_path: Path) -> None:
        with tmp_path.open("w") as f:
            _yaml.dump(data, f)

    atomic_write(path, _save, mode=0o600)


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password with bcrypt, returning the hash as a str."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def add_user(data, username: str, plaintext: str) -> None:
    """Add a new user to the in-memory users mapping.

    Raises UserError if the username already exists.
    """
    if username in data:
        raise UserError(f"user {username!r} already exists")
    data[username] = hash_password(plaintext)


def update_password(data, username: str, plaintext: str) -> None:
    """Update an existing user's password hash in the in-memory users mapping.

    Raises UserError if the username doesn't exist.
    """
    if username not in data:
        raise UserError(f"user {username!r} does not exist")
    data[username] = hash_password(plaintext)


def remove_user(data, username: str) -> None:
    """Remove a user from the in-memory users mapping.

    Raises UserError if the username doesn't exist.
    """
    if username not in data:
        raise UserError(f"user {username!r} does not exist")
    del data[username]
