"""Tests for prep.caddyconfig: Caddyfile generation, write, and reload.

generate_caddyfile()/write_caddyfile() tests build AlbumInfo/Manifest
objects directly rather than real fixture album folders, since Caddyfile
generation never reads from disk beyond the paths it's handed. reload_caddy()
tests mock subprocess.run directly -- per the plan, no test here requires a
real Caddy binary or container.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from click.testing import CliRunner

import prep.cli as cli_module
from prep.caddyconfig import (
    CADDYFILE_FILENAME,
    CaddyReloadError,
    generate_caddyfile,
    reload_caddy,
    write_caddyfile,
)
from prep.cli import cli
from prep.manifest import AlbumInfo, Manifest
from prep.render import AlbumSummary, write_album_summary

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _album(
    pictures_root: Path,
    rel_path: str,
    public: bool = False,
    allowed_users: list[str] | None = None,
) -> AlbumInfo:
    album_dir = pictures_root / rel_path
    album_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        album_dir=album_dir,
        title=rel_path,
        public=public,
        allowed_users=allowed_users or [],
        items=[],
    )
    return AlbumInfo(path=album_dir, manifest=manifest)


USERS = {
    "alice": "$2y$10$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "bob": "$2y$10$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
}


# -- generate_caddyfile ---------------------------------------------------


def test_public_album_has_no_basic_auth(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    album = _album(pictures_root, "2024/Public Trip", public=True)

    content = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    assert "handle_path /2024/Public Trip/*" in content or '"/2024/Public Trip/*"' in content
    # the album's own block must contain no basic_auth directive at all
    block_start = content.index("Public Trip")
    block = content[block_start:]
    assert "basic_auth" not in block


def test_private_album_contains_exactly_its_allowed_users(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    album = _album(pictures_root, "2024/Private Trip", public=False, allowed_users=["alice"])

    content = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    assert "basic_auth" in content
    assert "alice" in content
    assert USERS["alice"] in content
    assert "bob" not in content
    assert USERS["bob"] not in content


def test_private_album_drops_user_not_in_users_yml(tmp_path):
    """Defensive: validate_manifest (prep/manifest.py) already hard-fails
    publishing on an unknown-to-users.yml username upstream, but
    generate_caddyfile itself must never emit a basic_auth line for a
    username missing from `users` even if it somehow got this far."""
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    album = _album(
        pictures_root, "2024/Ghost", public=False, allowed_users=["alice", "nobody"]
    )

    content = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    assert "alice" in content
    assert "nobody" not in content


def test_every_album_route_excludes_work_and_publish_yaml_before_file_server(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    public_album = _album(pictures_root, "2024/A", public=True)
    private_album = _album(pictures_root, "2024/B", public=False, allowed_users=["alice"])

    content = generate_caddyfile(
        [public_album, private_album],
        USERS,
        "photos.example.com",
        "me@example.com",
        pictures_root,
        site_root,
    )

    # Every album block is wrapped in an explicit route {} containing the
    # @excluded matcher + respond, ordered before file_server.
    assert content.count("route {") == 2
    for block in content.split("handle_path")[1:]:
        assert "route {" in block
        excluded_idx = block.index("@excluded")
        respond_idx = block.index("respond @excluded 404")
        file_server_idx = block.index("file_server")
        assert excluded_idx < respond_idx < file_server_idx
        assert "/work/*" in block
        assert "/publish.yaml" in block


def _handle_path_patterns(content: str) -> list[str]:
    """Extract each `handle_path <pattern> {` directive's exact pattern
    token from generated Caddyfile content, in order of appearance."""
    patterns = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("handle_path "):
            # "handle_path <pattern> {" -- pattern is always the second
            # whitespace-separated token (never quoted in these tests,
            # since none of these fixture album paths need escaping).
            parts = stripped.split()
            assert parts[-1] == "{", f"unexpected handle_path line: {line!r}"
            patterns.append(parts[1])
    return patterns


def test_path_prefix_collision_pair_produces_non_overlapping_matchers(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    trip = _album(pictures_root, "2024/Trip", public=True)
    trip2 = _album(pictures_root, "2024/Trip2", public=True)

    content = generate_caddyfile(
        [trip, trip2], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    patterns = _handle_path_patterns(content)
    assert patterns == ["/2024/Trip/*", "/2024/Trip2/*"]
    # Each matcher is its own exact-path-plus-/* token -- /2024/Trip/* must
    # never be a *prefix* of /2024/Trip2/*'s own token (which it textually
    # is, as a substring, but the two parsed-out tokens must still be
    # distinct, whole, non-overlapping patterns handed to Caddy).
    assert patterns[0] != patterns[1]
    assert not patterns[1].startswith(patterns[0].rstrip("*"))


def test_album_route_redirects_bare_path_to_trailing_slash(tmp_path):
    """Caddy's path matcher requires the literal trailing slash before `*`
    in handle_path's pattern, so a bookmarked/typed album URL without a
    trailing slash (e.g. /2024/Trip, no trailing /) would otherwise fall
    through to the site-wide catch-all instead of the album -- verified
    live against a real caddy:2.8.4-alpine container (Implementation
    Correctness review finding #1). generate_caddyfile must emit an
    explicit redirect to the trailing-slash form ahead of each album's
    handle_path block."""
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    album = _album(pictures_root, "2024/Trip", public=True)

    content = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    assert "redir /2024/Trip /2024/Trip/ 308" in content
    # The redirect line must come before this album's handle_path block.
    redir_idx = content.index("redir /2024/Trip ")
    handle_idx = content.index("handle_path /2024/Trip/*")
    assert redir_idx < handle_idx


def test_album_route_redirect_escapes_space_in_album_path(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    album = _album(pictures_root, "2024/Public Trip", public=True)

    content = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    assert '"/2024/Public Trip" "/2024/Public Trip/" 308' in content


def test_private_album_with_empty_allowed_users_locks_out_everyone(tmp_path):
    """A private album (public=False) with an empty allowed_users list is
    deliberately not equivalent to a public album -- it must still emit a
    basic_auth block (with no accounts), so every request is rejected,
    rather than silently falling through to unauthenticated access (Test
    Coverage review finding #4)."""
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    album = _album(pictures_root, "2024/Nobody", public=False, allowed_users=[])

    content = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    block_start = content.index("handle_path")
    block = content[block_start:]
    assert "basic_auth {" in block
    # No username/hash lines at all inside this album's basic_auth block.
    auth_start = block.index("basic_auth {")
    auth_end = block.index("}", auth_start)
    auth_body = block[auth_start:auth_end]
    assert "alice" not in auth_body
    assert "bob" not in auth_body


def test_index_static_covers_remain_unauthenticated(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    private_album = _album(pictures_root, "2024/Private", public=False, allowed_users=["alice"])

    content = generate_caddyfile(
        [private_album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    static_block = content[content.index("handle /static/*") : content.index("handle /.covers/*")]
    covers_block = content[content.index("handle /.covers/*") : content.index("handle_path")]
    catch_all_block = content[content.rindex("handle {") :]
    for block in (static_block, covers_block, catch_all_block):
        assert "basic_auth" not in block


def test_generate_caddyfile_is_deterministic(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    album = _album(pictures_root, "2024/Trip", public=True)

    first = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )
    second = generate_caddyfile(
        [album], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )
    assert first == second


def test_generate_caddyfile_sorts_albums_regardless_of_input_order(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    a = _album(pictures_root, "2024/A", public=True)
    b = _album(pictures_root, "2024/B", public=True)

    forward = generate_caddyfile(
        [a, b], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )
    backward = generate_caddyfile(
        [b, a], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )
    assert forward == backward


# -- write_caddyfile -------------------------------------------------------


def test_write_caddyfile_atomic_write(tmp_path):
    dest = tmp_path / "caddy" / CADDYFILE_FILENAME
    write_caddyfile(dest, "example content\n")
    assert dest.read_text() == "example content\n"

    # overwrite
    write_caddyfile(dest, "replaced content\n")
    assert dest.read_text() == "replaced content\n"
    # no leftover temp files
    leftovers = [p for p in dest.parent.iterdir() if p.name != dest.name]
    assert leftovers == []


# -- reload_caddy -----------------------------------------------------------


def test_reload_caddy_skips_gracefully_when_container_not_running(monkeypatch):
    def fake_run(args, **kwargs):
        assert args[:3] == ["docker", "compose", "ps"]
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="no configuration file")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = reload_caddy()
    assert "skipped" in status.lower()
    assert "not running" in status.lower()


def test_reload_caddy_skips_gracefully_when_docker_missing(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = reload_caddy()
    assert "skipped" in status.lower()


def test_reload_caddy_raises_when_validate_fails_while_running(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(args, returncode=0, stdout="caddy\n", stderr="")
        if "validate" in args:
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="invalid Caddyfile")
        raise AssertionError(f"unexpected call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CaddyReloadError) as excinfo:
        reload_caddy()
    assert "invalid Caddyfile" in str(excinfo.value)
    # reload must never be attempted once validate fails
    assert not any("reload" in call for call in calls)


def test_reload_caddy_raises_when_reload_itself_fails_while_running(monkeypatch):
    def fake_run(args, **kwargs):
        if args[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(args, returncode=0, stdout="caddy\n", stderr="")
        if "validate" in args:
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")
        if "reload" in args:
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="reload boom")
        raise AssertionError(f"unexpected call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CaddyReloadError) as excinfo:
        reload_caddy()
    assert "reload boom" in str(excinfo.value)


def test_reload_caddy_succeeds_when_container_running_and_config_valid(monkeypatch):
    def fake_run(args, **kwargs):
        if args[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(args, returncode=0, stdout="caddy\n", stderr="")
        if "validate" in args:
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")
        if "reload" in args:
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")
        raise AssertionError(f"unexpected call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = reload_caddy()
    assert status == "reloaded Caddy"


# -- `prep sync-routes` CLI command -----------------------------------------


def test_sync_routes_writes_expected_caddyfile_for_published_albums_only(
    tmp_path, monkeypatch
):
    """discover+regenerate+write+validate+reload, covering only albums that
    have actually been published (have a .web/album.json) -- a bare
    publish.yaml with no album.json must be skipped, exactly like the
    albums index (task 10). reload_caddy() is stubbed -- no real Caddy
    container exists in this suite."""
    monkeypatch.setattr(
        cli_module, "reload_caddy", lambda *a, **kw: "reloaded Caddy (stub)"
    )

    pictures_root = tmp_path / "pictures"
    published_dir = pictures_root / "2024" / "Published"
    published_dir.mkdir(parents=True)
    (published_dir / "publish.yaml").write_text(
        "public: true\nallowed_users: []\n"
    )
    write_album_summary(
        published_dir,
        AlbumSummary(
            album_path="2024/Published",
            title="Published",
            public=True,
            earliest_date="2024-01-01",
            covers=[],
        ),
    )

    never_published_dir = pictures_root / "2024" / "Never Published"
    never_published_dir.mkdir(parents=True)
    (never_published_dir / "publish.yaml").write_text("public: true\n")

    site_root = tmp_path / "site"
    users_path = tmp_path / "users.yml"
    caddy_dir = tmp_path / "caddy"

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync-routes",
            "--pictures-root",
            str(pictures_root),
            "--site-root",
            str(site_root),
            "--caddy-dir",
            str(caddy_dir),
            "--users-file",
            str(users_path),
            "--domain",
            "photos.example.com",
            "--acme-email",
            "me@example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "reloaded Caddy (stub)" in result.output

    caddyfile = (caddy_dir / CADDYFILE_FILENAME).read_text()
    assert "/2024/Published/*" in caddyfile
    assert "Never Published" not in caddyfile
    assert "photos.example.com" in caddyfile


def test_sync_caddy_routes_skips_date_rescan_for_unlisted_media(tmp_path, monkeypatch):
    """Regression test for iteration-2 review finding #6: _published_albums
    (and therefore sync_caddy_routes, the shared tail of `prep sync-routes`/
    `users add`/`users passwd`/`users delete`) is supposed to pass
    populate_items=False through to discover_albums, since
    generate_caddyfile only ever reads each album's path/public/
    allowed_users, never its items. The existing sync-routes test above
    uses albums with zero real media files on disk, so the auto-population
    date-scan would never run anyway regardless of whether
    populate_items=False is actually wired through -- no real signal
    either way.

    This test drops a real, un-listed media file into a published album
    (no explicit `items:` in publish.yaml) and monkeypatches get_item_date
    to raise if it's ever called from prep.manifest's auto-population
    path. sync_caddy_routes() (called directly, not through the CLI, to
    exercise exactly the real call site named in the finding) must not
    raise.
    """
    import prep.manifest as manifest_module

    def _boom(*args, **kwargs):
        raise AssertionError(
            "get_item_date must not be called -- _published_albums should "
            "use populate_items=False"
        )

    monkeypatch.setattr(manifest_module, "get_item_date", _boom)
    monkeypatch.setattr(
        cli_module, "reload_caddy", lambda *a, **kw: "reloaded Caddy (stub)"
    )

    pictures_root = tmp_path / "pictures"
    album_dir = pictures_root / "2024" / "Trip"
    album_dir.mkdir(parents=True)
    (album_dir / "publish.yaml").write_text("public: true\nallowed_users: []\n")
    shutil.copy(FIXTURES / "sample_no_exif.jpg", album_dir / "IMG_0001.jpg")
    write_album_summary(
        album_dir,
        AlbumSummary(
            album_path="2024/Trip",
            title="Trip",
            public=True,
            earliest_date="2024-01-01",
            covers=[],
        ),
    )

    status = cli_module.sync_caddy_routes(
        pictures_root=pictures_root,
        site_root=tmp_path / "site",
        users_path=tmp_path / "users.yml",
        caddy_dir=tmp_path / "caddy",
        domain="photos.example.com",
        acme_email="me@example.com",
    )

    assert status == "reloaded Caddy (stub)"
    content = (tmp_path / "caddy" / CADDYFILE_FILENAME).read_text()
    assert "/2024/Trip" in content
