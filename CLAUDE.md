# CLAUDE.md

Repo-wide conventions for this project, for a future AI assistant/contributor
to pick up without re-deriving them from scratch. This is a knowledge base
for working in the code — for user-facing setup/usage docs, see `README.md`.
For the full design rationale ("why Caddy", "why no backend", etc.), see
`docs/plans/completed/20261024-photo-album-publisher.org`.

## What this is

A CLI (`prep`) that publishes curated photo albums (read from a real
`~/Pictures` tree) as a static HTML site, plus a generated Caddy config that
serves that tree directly with per-album HTTP Basic Auth. No database, no
backend service. `prep` is invoked ad hoc, never runs as a service; `caddy`
is the only long-running container.

## `prep` only ever runs inside its Docker container

Pillow, piexif, ffmpeg/ffprobe, bcrypt, etc. are **never installed on the
host** — every `prep` invocation goes through `docker compose run --rm prep
<args>` (see `prep/Dockerfile`). Don't suggest or attempt a host-side `pip
install`/`prep ...` invocation; it won't have ffmpeg and isn't the supported
path. Running the test suite directly in a host virtualenv (as this repo's
`.venv` does) is fine for development/review — only the real `prep` CLI
usage is container-only.

## Atomic-write-then-rename is mandatory for anything touching a real album

Every writer that touches a real album folder (or another persisted-output
file like `users.yml`) must write via a temp file in the destination's own
directory, then `os.replace`/rename into place — never a plain `open("w")`
on the real path. This is what makes a `prep publish` run crash-safe: a
crash mid-write leaves the destination exactly as it was, never truncated
or half-written.

The shared implementation is `prep/atomic.py`'s `atomic_write`/
`atomic_write_text` — call these directly (every module does; there are no
per-module aliases) rather than reimplementing the pattern. `atomic_write`
`chmod`s the file to `mode` before the rename (default `0o644`), since
`prep` runs as root in its container (no non-root `USER` is set in
`prep/Dockerfile` — see below) and the host user who manages the generated
tree is not root; without this, every generated file would default to
`tempfile.mkstemp`'s `0600` and be unreadable/unmanageable from the host.

**Exception: `users.yml`.** `prep/users.py`'s `save_users` passes an
explicit `mode=0o600` override. `users.yml` holds bcrypt password hashes
and is read only by the `prep` container itself, so — unlike
`publish.yaml`/HTML/thumbnails/the Caddyfile, which all need to be host- or
Caddy-readable at `0o644` — it must never be group/world-readable. If you
add a new writer for a credentials-bearing file, pass `mode=0o600`
explicitly; don't rely on the `0o644` default.

**If you add a new module that writes to a real album folder, `users.yml`,
or the generated `Caddyfile`, use `prep/atomic.py`, not a new copy of the
pattern.**

## Deterministic seeding for layout/scatter offsets

Anything that needs a reproducible-but-looks-random visual offset (irregular
grid placement in `prep/layout.py`'s `compute_grid_positions`, cover-thumbnail
scatter in `prep/render.py`'s `_cover_scatter_offset`) seeds
`random.Random(f"{album_path}:{index}")` (or a similar `f"{key}:{sub_key}"`
string) rather than using the global `random` module or an unseeded RNG.
This makes re-rendering an unchanged album byte-for-byte identical across
runs — required for the test suite's own determinism assertions, and
generally for not making every `prep publish` diff the entire site. Follow
this pattern for any new per-item/per-album randomized visual value.

## `style` attribute rule: custom properties only

No template or generated HTML may set a literal CSS property (`color`,
`display`, `width`, etc.) via a `style="..."` attribute. The *only* thing
ever allowed in a `style` attribute is one or more `--custom-property:
value;` declarations, consumed by a fixed rule in the relevant stylesheet
(e.g. `--row-span`/`--col-span` feeding a grid rule in `album-page.css`,
`--offset-x`/`--offset-y`/`--rotate` feeding a transform in
`albums-index.css`). This keeps all actual styling in versioned CSS files,
with templates only ever supplying *data* (numbers/positions), not style
decisions.

This is enforced by an automated test — `tests/test_render.py`'s
`test_style_attributes_set_only_custom_properties` and its per-page
counterparts (`test_render_album_page_only_custom_property_style_attributes`,
etc.) — which sweeps every shipped template/generated fixture HTML and fails
on any `style="..."` attribute containing anything other than
`--custom-property: value;` pairs. If you add a new template or a new
dynamic style value, make sure it still passes this sweep rather than
special-casing a literal property past it.

## Validation philosophy: hard-fail vs. warn

- **Hard failure (raises, aborts the whole `prep publish` run before
  anything is written for that album)**: an `allowed_users` entry not in
  `users.yml`; an `items` file entry that doesn't exist on disk; an `items`
  file entry that resolves inside `work/` (checked case-insensitively,
  matching Caddy's own case-insensitive `path` matcher) or outside the
  album directory entirely (path traversal); a corrupt/unreadable photo or
  video (Pillow/ffmpeg can't read it at all); an unsupported extension
  reached via an explicit `items` entry that `prep/metadata.py` can't date.
- **Warn and continue**: a photo/video with no capture-date metadata
  (EXIF `DateTimeOriginal` missing/unreadable, or no ffprobe
  `creation_time`) — falls back to file mtime, logged and collected into a
  warning summary printed at the end of an otherwise-successful run.

When adding a new validation rule, decide deliberately which bucket it
belongs in — this project has no silent middle ground ("skip it and move
on without telling anyone" is not an accepted behavior for anything that
could affect what gets published).

## Config loading: `prep.toml` / `tomllib`

`prep/cli.py`'s `_load_prep_config()` reads a gitignored `prep.toml` (the
checked-in `prep.example.toml` is the template) via the stdlib `tomllib`
(no extra TOML dependency). `_default_pictures_root`/`_default_site_domain`/
`_default_acme_email` resolve each CLI option's default from this file when
the corresponding `--pictures-root`/`--domain`/`--acme-email` flag isn't
passed explicitly. `pictures_root` has no safe default and raises
`click.UsageError` if unset either way; `site_domain`/`acme_email` fall back
to `"localhost"`/`"admin@localhost"` (fine for local/dev runs where Caddy
isn't reachable anyway).

## Caddyfile generation notes worth knowing before touching `prep/caddyconfig.py`

- `handle_path "<path>/*"` requires the literal trailing `/` before `*` —
  Caddy's path matcher does not treat a bare `/path` as matching `/path/*`.
  Every album route therefore also emits an explicit `redir <path> <path>/
  308` ahead of its `handle_path` block, so a bookmarked/typed URL without a
  trailing slash still resolves to the album instead of silently falling
  through to the site-wide catch-all. Verified against a real
  `caddy:2.8.4-alpine` container — don't remove this redirect without
  re-verifying against a real container.
- Caddy's `path` matcher is case-insensitive by design. Any Python-side
  check that mirrors Caddy's `work/`-exclusion behavior (manifest
  validation, album discovery, user-removal's manifest walk) must also be
  case-insensitive, or a case-variant directory (`Work/`) can validate/
  publish successfully in Python but then 404 at serve time.
- `generate_caddyfile` is pure (inputs in, Caddyfile text out); only
  `write_caddyfile`/`reload_caddy` touch the filesystem/Docker, so
  generation logic can be tested without a real Caddy container or
  `caddy/` directory.

## Running the test suite

- Host virtualenv (fast, but skips ffmpeg/ffprobe-dependent tests since
  neither is installed on the host by design): `pytest` from the repo root
  with `.venv` activated (or `.venv/bin/python -m pytest`).
- Full coverage, including the real-ffmpeg/ffprobe paths: `docker compose
  run --rm prep pytest` (or, for a one-off throwaway image without
  docker-compose, `docker build -f prep/Dockerfile -t <tag> .` then `docker
  run --rm --entrypoint pytest <tag>`).
- **Never point any test or manual verification at this host's real
  `~/Pictures`** — it's the actual user's ~27GB personal photo library.
  Always build a throwaway fixture pictures-root under `/tmp` (or a
  pytest `tmp_path`) for anything that needs a real pictures-root on disk.
- **Never stop/restart/remove a pre-existing Docker container** on this
  host while verifying something — this host's Docker daemon runs real,
  unrelated production containers alongside whatever you create. Only
  `docker build`/`docker run --rm <your-own-image>` or `docker compose`
  with overridden non-80/443 ports and a throwaway `PICTURES_ROOT`, cleaned
  up afterward, and confirm via `docker ps` that nothing pre-existing was
  touched.
