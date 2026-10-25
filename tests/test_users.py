"""Tests for prep.users: load/save round-trip, add_user, update_password."""

from __future__ import annotations

import bcrypt
import pytest
from click.testing import CliRunner

import prep.cli as cli_module
from prep.cli import cli
from prep.caddyconfig import CADDYFILE_FILENAME
from prep.manifest import load_manifest
from prep.users import (
    UserError,
    add_user,
    hash_password,
    load_users,
    remove_user,
    save_users,
    update_password,
)


def _invoke_users(monkeypatch, tmp_path, args, input=None):
    """Invoke a `users` subcommand with --pictures-root/--caddy-dir pointed
    at throwaway tmp dirs (so the task-12 Caddyfile regen+reload step at
    the end of `users add`/`users passwd` never touches the real repo's
    caddy/ directory), and reload_caddy() stubbed out -- this suite has no
    real Caddy container, and per the plan, tests must mock/stub
    reload_caddy() rather than exercise a real subprocess/docker call.
    """
    monkeypatch.setattr(cli_module, "reload_caddy", lambda *a, **kw: "reloaded Caddy (stub)")
    pictures_root = tmp_path / "pictures"
    pictures_root.mkdir(exist_ok=True)
    runner = CliRunner()
    return runner.invoke(
        cli,
        [
            *args,
            "--pictures-root",
            str(pictures_root),
            "--caddy-dir",
            str(tmp_path / "caddy"),
        ],
        input=input,
    )


def test_load_users_missing_file_returns_empty_mapping(tmp_path):
    path = tmp_path / "users.yml"
    data = load_users(path)
    assert dict(data) == {}


def test_save_and_load_round_trip_preserves_comments_and_order(tmp_path):
    path = tmp_path / "users.yml"
    path.write_text(
        "# account list\n"
        "alice: $2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "bob: $2b$12$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
    )

    data = load_users(path)
    assert list(data.keys()) == ["alice", "bob"]

    # modify one value and re-save; comment + ordering must survive.
    data["bob"] = "$2b$12$cccccccccccccccccccccccccccccccccccccccccccccccccccc"
    save_users(path, data)

    new_text = path.read_text()
    assert new_text.startswith("# account list\n")
    assert list(new_text.splitlines()[1:3]) == [
        "alice: $2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bob: $2b$12$cccccccccccccccccccccccccccccccccccccccccccccccccccc",
    ]

    reloaded = load_users(path)
    assert list(reloaded.keys()) == ["alice", "bob"]
    assert reloaded["bob"] == (
        "$2b$12$cccccccccccccccccccccccccccccccccccccccccccccccccccc"
    )


def test_hash_password_produces_verifiable_bcrypt_hash():
    hashed = hash_password("s3cret")
    assert hashed.startswith("$2")
    assert bcrypt.checkpw(b"s3cret", hashed.encode("utf-8"))
    # bcrypt hashes are salted, so hashing the same password twice differs.
    assert hash_password("s3cret") != hashed


def test_add_user_success():
    data = {}
    add_user(data, "alice", "s3cret")
    assert "alice" in data
    assert bcrypt.checkpw(b"s3cret", data["alice"].encode("utf-8"))


def test_add_user_duplicate_raises():
    data = {}
    add_user(data, "alice", "s3cret")
    with pytest.raises(UserError):
        add_user(data, "alice", "different")


def test_update_password_success():
    data = {}
    add_user(data, "alice", "old-pass")
    old_hash = data["alice"]
    update_password(data, "alice", "new-pass")
    assert data["alice"] != old_hash
    assert bcrypt.checkpw(b"new-pass", data["alice"].encode("utf-8"))


def test_update_password_unknown_user_raises():
    data = {}
    with pytest.raises(UserError):
        update_password(data, "nobody", "whatever")


def test_remove_user_success():
    data = {}
    add_user(data, "alice", "s3cret")
    remove_user(data, "alice")
    assert "alice" not in data


def test_remove_user_unknown_user_raises():
    data = {}
    with pytest.raises(UserError):
        remove_user(data, "nobody")


def test_cli_users_add_creates_user(tmp_path, monkeypatch):
    users_path = tmp_path / "users.yml"
    result = _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )
    assert result.exit_code == 0, result.output
    data = load_users(users_path)
    assert "alice" in data
    assert bcrypt.checkpw(b"s3cret", data["alice"].encode("utf-8"))
    assert "reloaded Caddy (stub)" in result.output


def test_cli_users_add_duplicate_errors(tmp_path, monkeypatch):
    users_path = tmp_path / "users.yml"
    _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )
    result = _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_cli_users_passwd_updates_password(tmp_path, monkeypatch):
    users_path = tmp_path / "users.yml"
    _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="old-pass\nold-pass\n",
    )
    data_before = load_users(users_path)
    old_hash = data_before["alice"]

    result = _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "passwd", "alice", "--users-file", str(users_path)],
        input="new-pass\nnew-pass\n",
    )
    assert result.exit_code == 0, result.output
    data_after = load_users(users_path)
    assert data_after["alice"] != old_hash
    assert bcrypt.checkpw(b"new-pass", data_after["alice"].encode("utf-8"))
    assert "reloaded Caddy (stub)" in result.output


def test_cli_users_passwd_unknown_user_errors(tmp_path, monkeypatch):
    users_path = tmp_path / "users.yml"
    result = _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "passwd", "nobody", "--users-file", str(users_path)],
        input="new-pass\nnew-pass\n",
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cli_users_delete_removes_user_from_users_yml_manifests_and_caddyfile(
    tmp_path, monkeypatch
):
    from prep.render import AlbumSummary, write_album_summary

    users_path = tmp_path / "users.yml"

    _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )
    _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "add", "bob", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )

    # _invoke_users always points --pictures-root at tmp_path / "pictures" --
    # build a published album manifest there listing both users, so this
    # test can assert the manifest AND the regenerated Caddyfile both
    # reflect alice's removal.
    pictures_root = tmp_path / "pictures"
    album_dir = pictures_root / "2024" / "Trip"
    album_dir.mkdir(parents=True)
    (album_dir / "publish.yaml").write_text(
        "# curated jointly\n"
        "title: Trip\n"
        "public: false\n"
        "allowed_users: [alice, bob]\n"
    )
    write_album_summary(
        album_dir,
        AlbumSummary(
            album_path="2024/Trip",
            title="Trip",
            public=False,
            earliest_date="2024-01-01",
            covers=[],
        ),
    )

    result = _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "delete", "alice", "--users-file", str(users_path)],
    )
    assert result.exit_code == 0, result.output
    assert "reloaded Caddy (stub)" in result.output
    assert str(album_dir) in result.output

    data = load_users(users_path)
    assert "alice" not in data
    assert "bob" in data

    manifest = load_manifest(album_dir)
    assert manifest.allowed_users == ["bob"]
    # Comment on the touched manifest must survive the round-trip edit.
    assert (album_dir / "publish.yaml").read_text().startswith("# curated jointly\n")

    caddyfile_content = (tmp_path / "caddy" / CADDYFILE_FILENAME).read_text()
    assert "alice" not in caddyfile_content


def test_cli_users_delete_unknown_user_errors(tmp_path, monkeypatch):
    users_path = tmp_path / "users.yml"
    result = _invoke_users(
        monkeypatch,
        tmp_path,
        ["users", "delete", "nobody", "--users-file", str(users_path)],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cli_hash_password_prints_hash():
    runner = CliRunner()
    result = runner.invoke(cli, ["hash-password"], input="s3cret\ns3cret\n")
    assert result.exit_code == 0, result.output
    printed = result.output.strip().splitlines()[-1]
    assert printed.startswith("$2")
    assert bcrypt.checkpw(b"s3cret", printed.encode("utf-8"))
