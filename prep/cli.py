"""click entrypoint: publish, sync-routes, hash-password, users add/passwd/delete."""

from __future__ import annotations

import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import click

from prep.assets import (
    AssetError,
    cleanup_stale_thumbnails,
    generate_photo_thumbnail,
    generate_video_poster,
    pick_cover_thumbnails,
    thumbnail_filename,
    thumbnail_path,
    thumbs_dir,
)
from prep.caddyconfig import (
    CADDYFILE_FILENAME,
    CaddyReloadError,
    generate_caddyfile,
    reload_caddy,
    write_caddyfile,
)
from prep.manifest import (
    AlbumInfo,
    ManifestError,
    ManifestItem,
    discover_albums,
    load_manifest,
    remove_user_from_manifests,
    validate_manifest,
)
from prep.metadata import VIDEO_EXTENSIONS, get_item_date
from prep.render import (
    COVERS_DIRNAME,
    AlbumSummary,
    load_album_summary,
    render_album_page,
    render_albums_index,
    render_picture_pages,
    sync_shared_static,
    write_album_summary,
)
from prep.users import (
    UserError,
    add_user,
    hash_password,
    load_users,
    remove_user,
    save_users,
    update_password,
)

DEFAULT_USERS_PATH = Path("users.yml")
DEFAULT_SITE_ROOT = Path("site")
DEFAULT_CADDY_DIR = Path("caddy")
DEFAULT_PREP_CONFIG_PATH = Path("prep.toml")


class PublishError(Exception):
    """Raised for `prep publish` orchestration failures (not manifest/asset-specific).

    ManifestError (unknown user, missing file, item inside work/) and
    AssetError (corrupt/unreadable source) are their own hard-failure types,
    raised by task 4/6's modules respectively and handled the same way by
    the CLI as this one — all three abort the run before any output is
    written for the album being published.
    """


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


def _load_prep_config(path: Path = DEFAULT_PREP_CONFIG_PATH) -> dict:
    """Read prep.toml (see prep.example.toml) if it exists, else {}."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _default_pictures_root() -> Path:
    """Resolve pictures_root from prep.toml when --pictures-root isn't given."""
    config = _load_prep_config()
    raw = config.get("pictures_root")
    if not raw:
        raise click.UsageError(
            "pictures_root is not configured; pass --pictures-root or set "
            "pictures_root in prep.toml (see prep.example.toml)."
        )
    return Path(raw)


def _default_site_domain() -> str:
    """Resolve site_domain from prep.toml when --domain isn't given.

    Unlike pictures_root (which has no safe default), a missing/unset
    domain just falls back to "localhost" -- fine for local/dev runs
    where Caddy isn't reachable anyway (see reload_caddy's graceful
    "container not running" skip), and overridable via prep.toml or
    --domain for a real deployment.
    """
    config = _load_prep_config()
    return str(config.get("site_domain") or "localhost")


def _default_acme_email() -> str:
    """Resolve acme_email from prep.toml when --acme-email isn't given."""
    config = _load_prep_config()
    return str(config.get("acme_email") or "admin@localhost")


def _caddy_sync_options(f):
    """Shared options for every command that ends with the Caddyfile
    regeneration+reload step (task 12): `publish`, `sync-routes`, `users
    add`, `users passwd`. Stacked as plain click.option decorators so each
    command gets the same five options with the same defaults/help text.
    """
    f = click.option(
        "--acme-email",
        "acme_email",
        default=None,
        help="ACME/Let's Encrypt contact email. Defaults to prep.toml's acme_email.",
    )(f)
    f = click.option(
        "--domain",
        "domain",
        default=None,
        help="Public domain Caddy serves. Defaults to prep.toml's site_domain.",
    )(f)
    f = click.option(
        "--caddy-dir",
        "caddy_dir",
        type=click.Path(path_type=Path),
        default=str(DEFAULT_CADDY_DIR),
        show_default=True,
        help="Directory the generated Caddyfile is written into.",
    )(f)
    f = click.option(
        "--site-root",
        "site_root",
        type=click.Path(path_type=Path),
        default=str(DEFAULT_SITE_ROOT),
        show_default=True,
        help="Site-wide output directory (index.html, static/, .covers/).",
    )(f)
    f = click.option(
        "--pictures-root",
        "pictures_root",
        type=click.Path(exists=True, file_okay=False, path_type=Path),
        default=None,
        help="Root directory containing all albums. Defaults to prep.toml's "
        "pictures_root.",
    )(f)
    return f


def _published_albums(pictures_root: Path) -> list[AlbumInfo]:
    """Every discovered album that has been published at least once (has a
    .web/album.json) -- the same "known/published albums" set Task 10's
    albums index and this task's Caddyfile both cover every route for."""
    return [
        album
        for album in discover_albums(pictures_root)
        if load_album_summary(album.path) is not None
    ]


def sync_caddy_routes(
    pictures_root: Path,
    site_root: Path,
    users_path: Path,
    caddy_dir: Path,
    domain: str,
    acme_email: str,
) -> str:
    """Regenerate, write, validate, and (if possible) reload the Caddyfile
    covering every known published album -- the shared final step for
    `prep publish`, `prep sync-routes`, `prep users add`, and `prep users
    passwd` (task 12). Touches no album image/HTML output.

    Returns reload_caddy()'s status string. Raises CaddyReloadError only
    when the Caddy container is confirmed running but rejects the
    generated config or the reload itself fails -- callers should turn
    that into a user-visible error, never swallow it.
    """
    pictures_root = Path(pictures_root).resolve()
    users = load_users(users_path)
    albums = _published_albums(pictures_root)
    content = generate_caddyfile(
        albums, users, domain, acme_email, pictures_root, Path(site_root)
    )
    write_caddyfile(Path(caddy_dir) / CADDYFILE_FILENAME, content)
    return reload_caddy()


def _sync_caddy_after_user_change(
    pictures_root: Path | None,
    site_root: Path,
    caddy_dir: Path,
    domain: str | None,
    acme_email: str | None,
    users_path: Path,
) -> None:
    """Shared tail end of `users add`/`users passwd`: regenerate+reload the
    Caddyfile so a password change invalidates the old password
    immediately, the same as a delete does (task 12/13)."""
    if pictures_root is None:
        pictures_root = _default_pictures_root()
    if domain is None:
        domain = _default_site_domain()
    if acme_email is None:
        acme_email = _default_acme_email()
    try:
        status = sync_caddy_routes(
            Path(pictures_root),
            Path(site_root),
            Path(users_path),
            Path(caddy_dir),
            domain,
            acme_email,
        )
    except CaddyReloadError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(status)


@users_group.command("add")
@click.argument("username")
@_caddy_sync_options
@click.option(
    "--users-file",
    "users_path",
    default=str(DEFAULT_USERS_PATH),
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to users.yml.",
)
def users_add(
    username: str,
    pictures_root: Path | None,
    site_root: Path,
    caddy_dir: Path,
    domain: str | None,
    acme_email: str | None,
    users_path: Path,
):
    """Add a new user, prompting for a password."""
    plaintext = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    data = load_users(users_path)
    try:
        add_user(data, username, plaintext)
    except UserError as exc:
        raise click.ClickException(str(exc)) from exc
    save_users(users_path, data)
    click.echo(f"Added user {username!r}.")

    _sync_caddy_after_user_change(
        pictures_root, site_root, caddy_dir, domain, acme_email, users_path
    )


@users_group.command("passwd")
@click.argument("username")
@_caddy_sync_options
@click.option(
    "--users-file",
    "users_path",
    default=str(DEFAULT_USERS_PATH),
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to users.yml.",
)
def users_passwd(
    username: str,
    pictures_root: Path | None,
    site_root: Path,
    caddy_dir: Path,
    domain: str | None,
    acme_email: str | None,
    users_path: Path,
):
    """Change an existing user's password, prompting for a new one."""
    plaintext = click.prompt("New password", hide_input=True, confirmation_prompt=True)
    data = load_users(users_path)
    try:
        update_password(data, username, plaintext)
    except UserError as exc:
        raise click.ClickException(str(exc)) from exc
    save_users(users_path, data)
    click.echo(f"Updated password for {username!r}.")

    _sync_caddy_after_user_change(
        pictures_root, site_root, caddy_dir, domain, acme_email, users_path
    )


@users_group.command("delete")
@click.argument("username")
@_caddy_sync_options
@click.option(
    "--users-file",
    "users_path",
    default=str(DEFAULT_USERS_PATH),
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to users.yml.",
)
def users_delete(
    username: str,
    pictures_root: Path | None,
    site_root: Path,
    caddy_dir: Path,
    domain: str | None,
    acme_email: str | None,
    users_path: Path,
):
    """Remove a user: from users.yml, from every album's allowed_users, and
    from the live Caddyfile -- revoking their access immediately.

    Errors clearly (without touching anything) if the username isn't in
    users.yml. Otherwise: deletes it from users.yml, strips it from every
    album manifest's allowed_users (task 13's remove_user_from_manifests,
    printing which albums were touched), then runs the same Caddyfile
    regeneration+reload path `sync-routes` uses so the deleted user's
    basic_auth credentials stop working immediately -- the same
    immediate-revocation guarantee `users passwd` already provides.
    """
    data = load_users(users_path)
    try:
        remove_user(data, username)
    except UserError as exc:
        raise click.ClickException(str(exc)) from exc
    save_users(users_path, data)
    click.echo(f"Removed user {username!r}.")

    if pictures_root is None:
        pictures_root = _default_pictures_root()

    touched = remove_user_from_manifests(username, Path(pictures_root))
    if touched:
        click.echo(f"Removed {username!r} from {len(touched)} album manifest(s):")
        for album_dir in touched:
            click.echo(f"  - {album_dir}")
    else:
        click.echo(f"{username!r} was not listed in any album's allowed_users.")

    _sync_caddy_after_user_change(
        pictures_root, site_root, caddy_dir, domain, acme_email, users_path
    )


@dataclass
class PublishResult:
    """Outcome of one `prep publish` run, for the CLI to report."""

    album_path: str
    warnings: list[str] = field(default_factory=list)


def _media_items(manifest) -> list[ManifestItem]:
    return [item for item in manifest.items if not item.is_note]


def _is_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def _generate_assets_atomically(
    album_dir: Path, media_items: list[ManifestItem]
) -> None:
    """Generate every item's thumbnail/poster into a staging dir, then move
    the whole batch into `.web/thumbs/` only if every item succeeded.

    This is what makes a Task 6 AssetError (corrupt/unreadable source)
    among several items a true hard-validation failure: if item 3 of 10
    fails, items 1-2's thumbnails are never moved into the real album
    folder at all — the staging directory (and everything in it) is
    discarded, leaving `.web/thumbs/` exactly as it was before this run.
    """
    web_dir = album_dir / ".web"
    web_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(web_dir), prefix=".thumbs-staging-"))
    try:
        for item in media_items:
            src = album_dir / item.file
            dest = staging / thumbnail_filename(item.file)
            if _is_video(item.file):
                generate_video_poster(src, dest)
            else:
                generate_photo_thumbnail(src, dest)

        final_dir = thumbs_dir(album_dir)
        final_dir.mkdir(parents=True, exist_ok=True)
        for item in media_items:
            filename = thumbnail_filename(item.file)
            (staging / filename).rename(final_dir / filename)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    cleanup_stale_thumbnails(album_dir, [item.file for item in media_items])


def _copy_covers(
    album_dir: Path,
    site_root: Path,
    album_rel_path: str,
    cover_items: list[ManifestItem],
) -> list[str]:
    """Copy the chosen cover thumbnails into the always-public
    <site_root>/.covers/<album_rel_path>/, removing any cover no longer
    chosen. Returns the plain filenames copied (AlbumSummary.covers)."""
    dest_dir = site_root / COVERS_DIRNAME / album_rel_path
    dest_dir.mkdir(parents=True, exist_ok=True)

    filenames: list[str] = []
    for item in cover_items:
        filename = thumbnail_filename(item.file)
        shutil.copy2(thumbnail_path(album_dir, item.file), dest_dir / filename)
        filenames.append(filename)

    keep = set(filenames)
    for existing in dest_dir.iterdir():
        if existing.is_file() and existing.name not in keep:
            existing.unlink()
    return filenames


def _publish_album(
    album_dir: Path,
    pictures_root: Path,
    site_root: Path,
    users_path: Path,
) -> PublishResult:
    """Run the full `prep publish` pipeline for one album.

    Order (per the plan's Processing Flow), through where Task 11 ends
    (Caddyfile regeneration + reload is wired in by Task 12):
    1. load + hard-validate the manifest, and resolve every item's date —
       entirely before writing anything for this album.
    2. generate thumbnails/posters (all-or-nothing, see
       _generate_assets_atomically) and copy chosen covers into the
       public site/.covers/.
    3. render the album page + picture pages, write .web/album.json.
    4. sync shared static assets and re-render the site-wide index across
       *all* published albums.

    Raises ManifestError / AssetError / PublishError on any hard failure,
    with nothing written for this album in that case.
    """
    album_dir = Path(album_dir).resolve()
    pictures_root = Path(pictures_root).resolve()
    site_root = Path(site_root)

    try:
        album_rel_path = album_dir.relative_to(pictures_root).as_posix()
    except ValueError as exc:
        raise PublishError(
            f"{album_dir} is not inside pictures root {pictures_root}"
        ) from exc

    # --- Phase 1: hard validation, zero writes -----------------------
    users = load_users(users_path)
    manifest = load_manifest(album_dir)
    validate_manifest(manifest, users)

    media_items = _media_items(manifest)

    warnings: list[str] = []
    dates: dict[str, date] = {}
    for item in media_items:
        try:
            item_datetime, is_fallback = get_item_date(album_dir / item.file)
        except ValueError as exc:
            # An explicit `items` entry can reference any extension that
            # exists on disk (validate_manifest only checks existence), but
            # one this module can't date at all would otherwise silently
            # produce a broken/undated tile — fail loudly instead.
            raise PublishError(f"{album_dir}: {exc}") from exc
        dates[item.file] = item_datetime.date()
        if is_fallback:
            warnings.append(
                f"{item.file}: no capture date metadata found; using file mtime"
            )

    # --- Phase 2: asset generation (all-or-nothing) -------------------
    _generate_assets_atomically(album_dir, media_items)

    cover_items = pick_cover_thumbnails(media_items)
    cover_filenames = _copy_covers(album_dir, site_root, album_rel_path, cover_items)

    # --- Phase 3: rendering --------------------------------------------
    album_info = AlbumInfo(path=album_dir, manifest=manifest)
    render_album_page(album_info, album_dir)
    render_picture_pages(album_info, album_dir)

    earliest_date = min(dates.values()) if dates else date.today()
    summary = AlbumSummary(
        album_path=album_rel_path,
        title=manifest.title,
        public=manifest.public,
        earliest_date=earliest_date.isoformat(),
        covers=cover_filenames,
    )
    write_album_summary(album_dir, summary)

    # --- Phase 4: site-wide refresh -------------------------------------
    sync_shared_static(site_root)
    render_albums_index(site_root, pictures_root)

    return PublishResult(album_path=album_rel_path, warnings=warnings)


@cli.command("publish")
@click.argument(
    "album_dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@_caddy_sync_options
@click.option(
    "--users-file",
    "users_path",
    default=str(DEFAULT_USERS_PATH),
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to users.yml.",
)
def publish(
    album_dir: Path,
    pictures_root: Path | None,
    site_root: Path,
    caddy_dir: Path,
    domain: str | None,
    acme_email: str | None,
    users_path: Path,
):
    """Validate, generate assets/pages for ALBUM_DIR, and refresh the site index.

    Hard-validation failures (unknown user, missing/work-dir item,
    corrupt/unreadable photo or video) abort before writing anything for
    this album. Recoverable issues (EXIF/video date fallback) are
    collected and printed as a warning summary at the end of a successful
    run.

    As its last step (task 12), regenerates the Caddyfile covering *all*
    known published albums (not just this one) and triggers a graceful
    reload -- tolerated gracefully (not an error) if the Caddy container
    isn't running.
    """
    if pictures_root is None:
        pictures_root = _default_pictures_root()
    if domain is None:
        domain = _default_site_domain()
    if acme_email is None:
        acme_email = _default_acme_email()

    try:
        result = _publish_album(
            Path(album_dir), Path(pictures_root), Path(site_root), Path(users_path)
        )
    except (ManifestError, AssetError, PublishError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Published {result.album_path}")
    if result.warnings:
        click.echo(f"{len(result.warnings)} warning(s):")
        for warning in result.warnings:
            click.echo(f"  - {warning}")

    try:
        status = sync_caddy_routes(
            Path(pictures_root),
            Path(site_root),
            Path(users_path),
            Path(caddy_dir),
            domain,
            acme_email,
        )
    except CaddyReloadError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(status)


@cli.command("sync-routes")
@_caddy_sync_options
@click.option(
    "--users-file",
    "users_path",
    default=str(DEFAULT_USERS_PATH),
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to users.yml.",
)
def sync_routes(
    pictures_root: Path | None,
    site_root: Path,
    caddy_dir: Path,
    domain: str | None,
    acme_email: str | None,
    users_path: Path,
):
    """Regenerate, validate, and (if possible) reload the Caddyfile for every
    known published album, without touching any album image/HTML output."""
    if pictures_root is None:
        pictures_root = _default_pictures_root()
    if domain is None:
        domain = _default_site_domain()
    if acme_email is None:
        acme_email = _default_acme_email()

    try:
        status = sync_caddy_routes(
            Path(pictures_root),
            Path(site_root),
            Path(users_path),
            Path(caddy_dir),
            domain,
            acme_email,
        )
    except CaddyReloadError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(status)


if __name__ == "__main__":
    cli()
