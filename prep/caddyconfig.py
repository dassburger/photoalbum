"""Caddyfile generation from users.yml and discovered album manifests.

See the plan's Solution Overview ("Why serve ~/Pictures directly") for the
exclusion mechanism this module encodes: a `path` matcher plus
`respond ... 404` for `work/` (any depth) and `publish.yaml`, wrapped in an
explicit `route { }` block so it always runs *before* `file_server` within
each album's route, regardless of Caddy's own default directive ordering.

generate_caddyfile() is pure (albums/users/config in, Caddyfile text out).
write_caddyfile() + reload_caddy() are the two side-effecting halves of
"regenerate and apply" — kept separate so tests can exercise generation
without ever touching a filesystem path meant for the real caddy/
directory, and so reload_caddy() alone can be mocked/stubbed everywhere a
real Caddy container isn't available (this dev sandbox has none — see
Task 17).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from prep.manifest import AlbumInfo, WORK_DIRNAME

# Caddyfile written into this directory (mounted at /etc/caddy in the caddy
# container per Task 17 — a directory, not a single bind-mounted file, so
# write_caddyfile's atomic temp-file-plus-rename doesn't leave the running
# container holding a stale inode).
CADDYFILE_FILENAME = "Caddyfile"

# Pinned Caddy image tag (NOT :latest) — Task 17's docker-compose.yml must
# reference this exact same tag. Generated `basic_auth` bcrypt-hash syntax
# acceptance has varied across Caddy versions; 2.8.4 is a recent, specific
# minor version confirmed to support the bcrypt `$2a$`/`$2b$`/`$2y$` hash
# syntax prep/users.py's bcrypt.hashpw output produces.
CADDY_IMAGE = "caddy:2.8.4-alpine"

CADDY_COMPOSE_SERVICE = "caddy"
CADDY_CONFIG_PATH_IN_CONTAINER = "/etc/caddy/Caddyfile"

PUBLISH_YAML_FILENAME = "publish.yaml"


class CaddyReloadError(Exception):
    """Raised only when the Caddy container IS confirmed running but
    `caddy validate`/`caddy reload` itself then fails — a real problem
    (a bug in our generated config, or an unexpected reload failure) that
    must be surfaced, never silently swallowed. The much more common
    "container isn't running at all" case (this dev sandbox, or any host
    before `docker compose up` has ever run) is NOT this error — see
    reload_caddy()."""


def _caddyfile_token(value: str) -> str:
    """Render `value` as a single Caddyfile token, double-quoting (with
    backslash/quote escaping) when it contains whitespace or a character
    that would otherwise split it into multiple tokens or confuse the
    Caddyfile parser (e.g. a space or unicode character in an album folder
    name) — see Task 12's "percent-encoded/escaped as needed" requirement.
    """
    if value == "" or any(ch.isspace() for ch in value) or '"' in value or "{" in value or "}" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _album_rel_path(album: AlbumInfo, pictures_root: Path) -> str:
    return album.path.resolve().relative_to(pictures_root.resolve()).as_posix()


def _album_route_path(rel_path: str) -> str:
    """URL path (leading slash, no trailing slash) this album is served
    under, derived from its path relative to pictures_root."""
    return "/" + rel_path


def _basic_auth_block(album: AlbumInfo, users: Mapping[str, str]) -> list[str]:
    """`username bcrypt_hash` lines for exactly this album's
    allowed_users ∩ users.yml — no `basic_auth` block at all for public
    albums. Unknown-to-users.yml usernames are already a hard
    publish-time validation error (prep/manifest.py's validate_manifest),
    so the intersection here is a defensive no-op in the normal case, not
    a substitute for that check.
    """
    if album.manifest.public:
        return []
    lines = ["    basic_auth {"]
    for username in album.manifest.allowed_users:
        password_hash = users.get(username)
        if password_hash is None:
            continue
        lines.append(f"        {_caddyfile_token(username)} {_caddyfile_token(str(password_hash))}")
    lines.append("    }")
    return lines


def _album_block(album: AlbumInfo, users: Mapping[str, str], pictures_root: Path) -> list[str]:
    rel_path = _album_rel_path(album, pictures_root)
    route_path = _album_route_path(rel_path)
    root_path = str(album.path.resolve())

    lines = [f"    handle_path {_caddyfile_token(route_path + '/*')} {{"]
    lines.append(f"        root * {_caddyfile_token(root_path)}")
    lines.append("        route {")
    lines.extend("        " + line for line in _basic_auth_block(album, users))
    lines.append(f"            @excluded path /{WORK_DIRNAME}/* /{PUBLISH_YAML_FILENAME}")
    lines.append("            respond @excluded 404")
    lines.append("            file_server")
    lines.append("        }")
    lines.append("    }")
    return lines


def generate_caddyfile(
    albums: Sequence[AlbumInfo],
    users: Mapping[str, str],
    domain: str,
    acme_email: str,
    pictures_root: str | Path,
    site_root: str | Path,
) -> str:
    """Produce the full Caddyfile content: one site block for `domain`.

    Contains, in order:
    - an unauthenticated `handle` for `/static/*` and `/.covers/*`, rooted
      at `site_root` (never behind basic_auth — see Solution Overview's
      "Index privacy tradeoff" and Task 10)
    - one `handle_path` route per album in `albums`, sorted by path for
      deterministic output, each wrapped in a `route { }` block so the
      work/publish.yaml exclusion always runs before `file_server`
      regardless of Caddy's default directive sorting, and wrapped in a
      `basic_auth` block for private albums only
    - a final catch-all `handle` (bare, no path matcher) serving `/` and
      everything else (the albums index itself) from `site_root`

    `albums` should already be filtered to "published at least once"
    (callers use prep/cli.py's `_published_albums`) — this function does
    not re-check that itself.
    """
    pictures_root = Path(pictures_root)
    site_root = Path(site_root)
    site_root_token = _caddyfile_token(str(site_root.resolve()))

    sorted_albums = sorted(albums, key=lambda a: _album_rel_path(a, pictures_root))

    lines: list[str] = [f"{domain} {{", f"    tls {acme_email}", ""]

    lines.append("    handle /static/* {")
    lines.append(f"        root * {site_root_token}")
    lines.append("        file_server")
    lines.append("    }")
    lines.append("    handle /.covers/* {")
    lines.append(f"        root * {site_root_token}")
    lines.append("        file_server")
    lines.append("    }")
    lines.append("")

    for album in sorted_albums:
        lines.extend(_album_block(album, users, pictures_root))
        lines.append("")

    lines.append("    handle {")
    lines.append(f"        root * {site_root_token}")
    lines.append("        file_server")
    lines.append("    }")
    lines.append("}")
    lines.append("")

    return "\n".join(lines)


def write_caddyfile(path: str | Path, content: str) -> Path:
    """Write `content` to `path` atomically (temp file + rename), so the
    directory `path` lives in (mounted at /etc/caddy in the caddy
    container — see CADDYFILE_FILENAME) never holds a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def _docker_compose_exec(service: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "exec", service, *args],
        capture_output=True,
        text=True,
    )


def _is_caddy_container_running(service: str = CADDY_COMPOSE_SERVICE) -> bool:
    """True only if `docker compose` reports `service` as a currently
    running container. Never raises — every "not available" case this dev
    sandbox hits (docker not installed, no compose project file here yet,
    or the service simply isn't up) returns False, which reload_caddy()
    treats as a single tolerated "skip reload" case rather than a crash.
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--services"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    running_services = set(result.stdout.split())
    return service in running_services


def reload_caddy(
    caddyfile_container_path: str = CADDY_CONFIG_PATH_IN_CONTAINER,
    service: str = CADDY_COMPOSE_SERVICE,
) -> str:
    """Validate then gracefully reload the Caddy container's live config.

    Returns a human-readable status string:
    - "reloaded Caddy" on success
    - a "skipped Caddy reload: ..." message, NOT an error, when the caddy
      container isn't currently running (this dev sandbox has no Caddy at
      all yet — Task 17 — and any real host before its first
      `docker compose up` is in the same state)

    Raises CaddyReloadError only when the container IS running but
    `caddy validate` reports the generated Caddyfile itself is invalid, or
    `caddy reload` fails for some other reason — a real problem that must
    never be silently swallowed.
    """
    if not _is_caddy_container_running(service):
        return (
            f"skipped Caddy reload: the {service!r} container is not running "
            "(expected until `docker compose up` has been run — see Task 17)"
        )

    validate_result = _docker_compose_exec(
        service, "caddy", "validate", "--config", caddyfile_container_path
    )
    if validate_result.returncode != 0:
        detail = validate_result.stderr.strip() or validate_result.stdout.strip()
        raise CaddyReloadError(f"generated Caddyfile failed `caddy validate`: {detail}")

    reload_result = _docker_compose_exec(
        service, "caddy", "reload", "--config", caddyfile_container_path
    )
    if reload_result.returncode != 0:
        detail = reload_result.stderr.strip() or reload_result.stdout.strip()
        raise CaddyReloadError(f"`caddy reload` failed: {detail}")

    return "reloaded Caddy"
