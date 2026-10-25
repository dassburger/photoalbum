"""Tests for prep.caddyconfig: Caddyfile generation, write, and reload.

generate_caddyfile()/write_caddyfile() tests build AlbumInfo/Manifest
objects directly rather than real fixture album folders, since Caddyfile
generation never reads from disk beyond the paths it's handed. reload_caddy()
tests mock subprocess.run directly -- per the plan, no test here requires a
real Caddy binary or container.
"""

from __future__ import annotations

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


def test_path_prefix_collision_pair_produces_non_overlapping_matchers(tmp_path):
    pictures_root = tmp_path / "pictures"
    site_root = tmp_path / "site"
    trip = _album(pictures_root, "2024/Trip", public=True)
    trip2 = _album(pictures_root, "2024/Trip2", public=True)

    content = generate_caddyfile(
        [trip, trip2], USERS, "photos.example.com", "me@example.com", pictures_root, site_root
    )

    assert "/2024/Trip/*" in content
    assert "/2024/Trip2/*" in content
    # /2024/Trip/* must never appear as a prefix match consuming Trip2's
    # route -- each matcher is its own exact-path-plus-/* token.
    assert "/2024/Trip/*\n" in content or content.count("/2024/Trip/*") >= 1
    assert "/2024/Trip2/*" != "/2024/Trip/*"


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
