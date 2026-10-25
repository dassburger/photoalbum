"""Tests for prep.users: load/save round-trip, add_user, update_password."""

from __future__ import annotations

import bcrypt
import pytest
from click.testing import CliRunner

from prep.cli import cli
from prep.users import (
    UserError,
    add_user,
    hash_password,
    load_users,
    save_users,
    update_password,
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


def test_cli_users_add_creates_user(tmp_path):
    users_path = tmp_path / "users.yml"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )
    assert result.exit_code == 0, result.output
    data = load_users(users_path)
    assert "alice" in data
    assert bcrypt.checkpw(b"s3cret", data["alice"].encode("utf-8"))


def test_cli_users_add_duplicate_errors(tmp_path):
    users_path = tmp_path / "users.yml"
    runner = CliRunner()
    runner.invoke(
        cli,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )
    result = runner.invoke(
        cli,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="s3cret\ns3cret\n",
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_cli_users_passwd_updates_password(tmp_path):
    users_path = tmp_path / "users.yml"
    runner = CliRunner()
    runner.invoke(
        cli,
        ["users", "add", "alice", "--users-file", str(users_path)],
        input="old-pass\nold-pass\n",
    )
    data_before = load_users(users_path)
    old_hash = data_before["alice"]

    result = runner.invoke(
        cli,
        ["users", "passwd", "alice", "--users-file", str(users_path)],
        input="new-pass\nnew-pass\n",
    )
    assert result.exit_code == 0, result.output
    data_after = load_users(users_path)
    assert data_after["alice"] != old_hash
    assert bcrypt.checkpw(b"new-pass", data_after["alice"].encode("utf-8"))


def test_cli_users_passwd_unknown_user_errors(tmp_path):
    users_path = tmp_path / "users.yml"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["users", "passwd", "nobody", "--users-file", str(users_path)],
        input="new-pass\nnew-pass\n",
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
