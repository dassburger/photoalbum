"""click entrypoint: publish, sync-routes, hash-password, users add/passwd/delete."""

from __future__ import annotations

from pathlib import Path

import click

from prep.users import (
    UserError,
    add_user,
    hash_password,
    load_users,
    save_users,
    update_password,
)

DEFAULT_USERS_PATH = Path("users.yml")


@click.group()
def cli():
    """prep: publish curated photo albums as a static site."""


@cli.command("hash-password")
def hash_password_cmd():
    """Prompt for a password and print its bcrypt hash."""
    plaintext = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    click.echo(hash_password(plaintext))


@cli.group("users")
def users_group():
    """Manage users.yml accounts."""


@users_group.command("add")
@click.argument("username")
@click.option(
    "--users-file",
    "users_path",
    default=str(DEFAULT_USERS_PATH),
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to users.yml.",
)
def users_add(username: str, users_path: Path):
    """Add a new user, prompting for a password."""
    plaintext = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    data = load_users(users_path)
    try:
        add_user(data, username, plaintext)
    except UserError as exc:
        raise click.ClickException(str(exc)) from exc
    save_users(users_path, data)
    click.echo(f"Added user {username!r}.")


@users_group.command("passwd")
@click.argument("username")
@click.option(
    "--users-file",
    "users_path",
    default=str(DEFAULT_USERS_PATH),
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to users.yml.",
)
def users_passwd(username: str, users_path: Path):
    """Change an existing user's password, prompting for a new one."""
    plaintext = click.prompt("New password", hide_input=True, confirmation_prompt=True)
    data = load_users(users_path)
    try:
        update_password(data, username, plaintext)
    except UserError as exc:
        raise click.ClickException(str(exc)) from exc
    save_users(users_path, data)
    click.echo(f"Updated password for {username!r}.")


if __name__ == "__main__":
    cli()
