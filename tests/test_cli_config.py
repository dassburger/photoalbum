"""Tests for prep/cli.py's config-resolution helpers: _load_prep_config,
_default_pictures_root, _default_site_domain, _default_acme_email.

These had zero test coverage at all before this review (Test Coverage
review finding #2) despite being the first thing every `prep` command
touches when a --pictures-root/--domain/--acme-email flag isn't passed
explicitly.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from prep.cli import (
    _default_acme_email,
    _default_pictures_root,
    _default_site_domain,
    _load_prep_config,
)


# -- _load_prep_config -------------------------------------------------------


def test_load_prep_config_returns_empty_dict_when_file_missing(tmp_path):
    assert _load_prep_config(tmp_path / "does-not-exist.toml") == {}


def test_load_prep_config_parses_real_toml_file(tmp_path):
    config_path = tmp_path / "prep.toml"
    config_path.write_text(
        'pictures_root = "/pictures"\n'
        'site_domain = "photos.example.com"\n'
        'acme_email = "me@example.com"\n'
    )

    config = _load_prep_config(config_path)

    assert config == {
        "pictures_root": "/pictures",
        "site_domain": "photos.example.com",
        "acme_email": "me@example.com",
    }


# -- _default_pictures_root --------------------------------------------------


def test_default_pictures_root_raises_usage_error_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No prep.toml at all in this throwaway cwd.
    with pytest.raises(click.UsageError, match="pictures_root"):
        _default_pictures_root()


def test_default_pictures_root_reads_from_prep_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prep.toml").write_text('pictures_root = "/pictures"\n')

    assert _default_pictures_root() == Path("/pictures")


def test_default_pictures_root_raises_when_prep_toml_lacks_the_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prep.toml").write_text('site_domain = "example.com"\n')

    with pytest.raises(click.UsageError, match="pictures_root"):
        _default_pictures_root()


# -- _default_site_domain ----------------------------------------------------


def test_default_site_domain_falls_back_to_localhost_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _default_site_domain() == "localhost"


def test_default_site_domain_reads_from_prep_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prep.toml").write_text('site_domain = "photos.example.com"\n')

    assert _default_site_domain() == "photos.example.com"


# -- _default_acme_email -----------------------------------------------------


def test_default_acme_email_falls_back_to_admin_localhost_when_unconfigured(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert _default_acme_email() == "admin@localhost"


def test_default_acme_email_reads_from_prep_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prep.toml").write_text('acme_email = "me@example.com"\n')

    assert _default_acme_email() == "me@example.com"
