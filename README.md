# Photo Album Publisher

Publish curated albums from a `~/Pictures` photo archive as a
password-protected (with optional public albums) responsive web photo
gallery. Two moving parts, both run via `docker compose`:

- **`prep`** — a Python CLI, run by hand inside its own container, that
  reads per-album `publish.yaml` manifests, generates thumbnails/poster
  frames and static HTML directly inside each album's own folder, and
  regenerates Caddy's routing/auth config. Not a running service — invoked
  ad hoc.
- **`caddy`** — the only continuously-running container. Serves the real
  `~/Pictures` tree (plus a small site-wide output directory) over HTTPS
  with automatic Let's Encrypt renewal, per-album HTTP Basic Auth, and a
  block on work-in-progress content.

No database, no backend service, no web admin UI. User accounts and
per-album access lists are plain YAML, hand-edited and/or CLI-managed.
Original photos/videos are never copied or modified by `prep`; Caddy serves
them directly from `~/Pictures`. See `docs/plans/completed/` for the full
design rationale (why Caddy, why no backend, why no symlink mirror, etc.)
if you want the "why" behind any of this.

## Host prerequisites

Only **Docker and Docker Compose** need to be installed on the host.
Python, Pillow, and ffmpeg are never installed on the host — every `prep`
invocation runs inside its own container (see `prep/Dockerfile`), built
locally and never pushed to a registry.

## First-time local setup

Run these from the repo root, in order:

1. **Build the `prep` image** (also pulls in `pytest`, so the same image
   can run the test suite if you ever want to):

   ```sh
   docker compose build prep
   ```

2. **Create `users.yml`** by adding your first user (prompts for a
   password; nothing is written to disk until this succeeds):

   ```sh
   docker compose run --rm prep users add alice
   ```

   This creates `users.yml` in the repo root if it doesn't exist yet.
   Passwords are never stored — only bcrypt hashes.

3. **Write `prep.toml`** from the checked-in example:

   ```sh
   cp prep.example.toml prep.toml
   ```

   Edit it to set:
   - `site_domain` — the public domain name Caddy will serve (used for
     Let's Encrypt). For a first local check this can stay as the example
     value or be set to `localhost`.
   - `acme_email` — the email Let's Encrypt uses for expiry/revocation
     notices.
   - `pictures_root` — **the container-internal mount path**, not a host
     path. This must match what `docker-compose.yml` mounts the archive at
     inside both containers: `/pictures`. (The real host-side path is set
     separately via the `PICTURES_ROOT` environment variable — see below.)

   `prep.toml` is gitignored; only `prep.example.toml` is checked in.

4. **Put a `publish.yaml` in at least one album folder** under the real
   `~/Pictures` tree, e.g. `~/Pictures/2024/family-reunion/publish.yaml`:

   ```yaml
   title: "Family Reunion 2024"
   public: false
   allowed_users: [alice]
   ```

   (`items` can be omitted — `prep` auto-populates it from the folder's
   JPEGs/videos, sorted by capture date, excluding anything under a
   `work/` subdirectory. See "The `work/` convention" below.)

5. **Run `prep publish` on that album**:

   ```sh
   docker compose run --rm prep publish /pictures/2024/family-reunion
   ```

   This validates the manifest, generates thumbnails/poster-frames and the
   album's `index.html`/`picture/*.html` pages directly inside that folder,
   refreshes the site-wide albums index, and (as its last step) regenerates
   the Caddyfile and reloads Caddy — gracefully skipped with a message if
   Caddy isn't running yet, which is expected the very first time.

6. **Bring Caddy up**:

   ```sh
   docker compose up -d caddy
   ```

   The `prep` service is deliberately *not* started by `docker compose up`
   (it's excluded via a `profiles: [tools]` entry in `docker-compose.yml`)
   — it only ever runs via `docker compose run --rm prep <args>`. From here
   on, `prep publish`'s own reload step (step 5) will find Caddy running
   and reload it for real on every subsequent run.

### Environment variables

`docker-compose.yml` reads these (a gitignored `.env` file in the repo root
works too):

| Variable | Default | Meaning |
|---|---|---|
| `PICTURES_ROOT` | this host's real `~/Pictures` path | Host path to the photo archive, bind-mounted into both containers at `/pictures` |
| `CADDY_HTTP_PORT` | `80` | Host port published for Caddy's HTTP listener |
| `CADDY_HTTPS_PORT` | `443` | Host port published for Caddy's HTTPS listener |

For a throwaway local verification run against sample data instead of your
real archive, override `PICTURES_ROOT` (and the ports, if 80/443 are
already taken on this host):

```sh
PICTURES_ROOT=/tmp/fixture-pictures CADDY_HTTP_PORT=18080 CADDY_HTTPS_PORT=18443 docker compose up -d caddy
```

## Day-to-day curator workflow

1. Edit `publish.yaml` in an album folder (add/reorder `items`, add a
   `note`, change `public`/`allowed_users`, change the title).
2. Run:

   ```sh
   docker compose run --rm prep publish ~/Pictures/2024/family-reunion
   ```

   (or the container-internal path, `/pictures/2024/family-reunion`, if
   you're already inside a shell in the `prep` container).

3. Changes go live immediately — no restart needed. `prep publish` ends by
   regenerating the Caddyfile for *every* known album and triggering
   `caddy reload` inside the running container.

Hard-validation failures (an `allowed_users` entry not in `users.yml`, an
`items` entry that doesn't exist on disk or that points inside `work/`, a
corrupt/unreadable photo or video) abort the run *before* anything is
written for that album — nothing partial ever lands in your real photo
folder. Recoverable issues (a photo/video with no capture-date metadata,
falling back to file mtime) are printed as a warning summary at the end of
an otherwise-successful run.

## User management

All of these are `docker compose run --rm prep ...` invocations from the
repo root:

```sh
# Print a bcrypt hash for a password you type (useful for manual editing,
# not required for normal use since `users add`/`passwd` hash for you).
docker compose run --rm prep hash-password

# Add a new user (prompts for a password).
docker compose run --rm prep users add alice

# Change an existing user's password (prompts for a new one).
docker compose run --rm prep users passwd alice

# Remove a user entirely.
docker compose run --rm prep users delete alice
```

`users add` and `users passwd` both end by regenerating and reloading the
Caddyfile, same as `prep publish`.

`users delete` additionally strips the username from every album's
`allowed_users` in its `publish.yaml` (printing which albums were touched)
and then regenerates/reloads the Caddyfile — so **access is revoked
immediately**, with no stale Basic Auth session able to keep working:
cached browser credentials for that realm stop being accepted the moment
the reload completes, exactly like a password change.

If you ever want to regenerate/reload the Caddyfile without touching any
album's images or HTML (e.g. after hand-editing `users.yml` or a
`publish.yaml`'s `allowed_users` directly), run:

```sh
docker compose run --rm prep sync-routes
```

## The `publish.yaml` manifest: `items` and `note` entries

Beyond `title`/`public`/`allowed_users`, `publish.yaml` can set an explicit
`items` list to control exactly what's shown and in what order — mixing
photo/video file references with standalone text notes:

```yaml
title: "Family Reunion 2024"
public: false
allowed_users: [alice, bob]
items:
  - file: IMG_0001.jpg
  - file: IMG_0002.jpg
  - note: "This was the day it rained all afternoon."
  - file: VID_0003.mp4
```

**An explicit `items` list is authoritative and is never re-sorted
chronologically** — the order you write is the order rendered, even if a
later item's capture date is actually earlier than one listed before it.
This is what lets you tell a story out of strict date order.

If `items` is omitted entirely, `prep` auto-populates it by scanning the
album folder (non-recursively, excluding `work/`) for files with a
supported extension and sorting them by capture date. As of this writing,
the supported extensions (`prep/metadata.py`'s `PHOTO_EXTENSIONS`/
`VIDEO_EXTENSIONS` — check that file directly if this drifts) are:

- Photos: `.jpg`, `.jpeg`
- Video: `.mp4`, `.mov`, `.avi`, `.mkv`

Any other file sitting loose in the album folder (a `.png`, a raw `.CR2`,
a Darktable `.xmp`, etc.) is silently skipped during auto-population — it
won't appear in the album, but see the caveat at the end of "The `work/`
convention" below about it still being directly fetchable by URL. A `note`
entry has no backing file at all; it renders as inline text and has no
cover-thumbnail eligibility.

## The `work/` convention

Each album folder may contain a fixed-name `work/` subdirectory for CR2
raw files, Darktable project files (`.xmp`, project database, etc.), and
rejected JPEGs — anything not ready to publish. `prep` never looks inside
`work/` when auto-populating `items`, and explicitly refuses (hard
validation error) to publish an `items` entry that points inside it.

Caddy's generated routing additionally blocks `work/` (at any nesting
depth, case-insensitively) and the album's own `publish.yaml` file with a
`path` matcher plus `respond ... 404`, evaluated *before* `file_server`
runs — so a request for `/2024/family-reunion/work/raw.cr2` or
`/2024/family-reunion/Work/raw.cr2` 404s regardless of case or depth, and
`publish.yaml` itself is never web-reachable.

**Important caveat**: this is a workflow convention backed by Caddy's
`work/`/`publish.yaml` block, *not* a guarantee that every other file in an
album folder is unreachable. Any file inside an album folder that is
*outside* `work/` and isn't `publish.yaml` — including a stray, un-curated
JPEG you dropped in the folder but never added to `items` — is directly
fetchable by anyone who knows or guesses its exact URL, even though it
appears on no page and is linked from nowhere. This system deliberately
serves the real `~/Pictures` tree directly rather than building a
per-file symlink whitelist mirror, in exchange for a much simpler system
(see the design plan's "Why serve `~/Pictures` directly" discussion). In
practice: **put anything not ready to be seen by anyone inside `work/`**;
don't rely on "I just didn't list it in `items`" as a privacy boundary for
files living loose in the album folder itself.

## Index privacy tradeoff: what a private album leaks on the public index

The site-wide albums index has to show every album's title and a few cover
thumbnails to work as a single, unauthenticated public landing page
(including for anonymous visitors and as the logout redirect target) — but
a private album's cover thumbnails live inside that album's auth-protected
folder. To resolve this, `prep publish` copies each album's chosen cover
thumbnails into a separate, always-public `site/.covers/<album-path>/`
directory, served by Caddy with no `basic_auth` wrapper at all.

**This means a private album's title and 2-3 small cover thumbnails are
visible to anyone who can reach the index at all** — not the full album
content, which stays behind that album's password, but enough to see that
the album exists, its name, and a handful of its photos. This is a
deliberate, modest metadata leak, consistent with this project's low-stakes
"family-shared album" risk tolerance. If that tradeoff isn't acceptable for
a particular album, don't publish it at all rather than relying on
`allowed_users` to keep its existence or thumbnails fully hidden.

## Remote admin via Claude Code with Remote Control

There is no bespoke web admin UI. Instead, occasional administrative
adjustments are made by starting a [Claude Code](https://claude.com/claude-code)
session in this project directory with Remote Control enabled, from
whatever device you have at hand (phone, laptop away from home, etc.), and
asking it to make the change conversationally.

**Safe/expected to delegate to a remote Claude Code session:**

- User management: `prep users add/passwd/delete`.
- Editing a `publish.yaml` already sitting under `~/Pictures` (change
  `allowed_users`, add a `note`, reorder `items`, flip `public`).
- Re-running `prep publish <album>` or `prep sync-routes` after an edit.
- Reading back `prep publish`'s warning output or `prep sync-routes`'
  generated Caddyfile to sanity-check a change before/after it goes live.
- Restarting the `caddy` service (`docker compose restart caddy`) if a
  reload-tolerant issue needs a harder bounce.

**Still needs hands-on access to this machine:**

- Physically placing new photos/videos into `~/Pictures` (copying off a
  camera/phone, organizing into a new album folder, moving raw CR2s into
  `work/`).
- Any `socat`/WireGuard tunnel troubleshooting that requires touching this
  machine's actual network interfaces or physically rebooting it.
- Anything requiring interactive terminal input Claude Code's Remote
  Control can't supply (there isn't much of this left, since `prep`'s
  commands are all either argument-driven or use simple hidden-input
  prompts).

This requires no new code — it's a documented operating pattern, not a
feature of `prep` itself.

## Remote access: tunnel + VPN + TLS

This application is hosted on this machine (not a VPS), and made reachable
from the internet through a DigitalOcean host acting as a relay, reached
over a WireGuard VPN tunnel, with a `socat` TCP relay on the DigitalOcean
side forwarding public ports 80/443 back through that tunnel to this
machine's Caddy container.

```
  Internet  --->  DigitalOcean host  ===WireGuard===  this machine
                  (socat :80/:443)                    (Caddy :80/:443)
```

### 1. WireGuard tunnel between this machine and the DigitalOcean host

On the DigitalOcean host (acting as the WireGuard server):

```sh
apt-get install wireguard
wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
```

Create `/etc/wireguard/wg0.conf` on the DO host:

```ini
[Interface]
Address = 10.10.0.1/24
PrivateKey = <DO-host-private-key>
ListenPort = 51820

[Peer]
# this machine
PublicKey = <this-machine-public-key>
AllowedIPs = 10.10.0.2/32
```

On this machine (the WireGuard client):

```sh
apt-get install wireguard
wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
```

Create `/etc/wireguard/wg0.conf` on this machine:

```ini
[Interface]
Address = 10.10.0.2/24
PrivateKey = <this-machine-private-key>

[Peer]
PublicKey = <DO-host-public-key>
Endpoint = <DO-host-public-ip>:51820
AllowedIPs = 10.10.0.1/32
PersistentKeepalive = 25
```

Exchange the two public keys between the files above, open UDP port 51820
on the DO host's firewall, then bring the interface up on both ends:

```sh
wg-quick up wg0
```

Verify with `wg show` on both sides — each should show the other as a
peer with a recent handshake. Enable at boot on both ends with
`systemctl enable wg-quick@wg0`.

### 2. `socat` relay on the DigitalOcean host

Once the WireGuard tunnel is up, this machine is reachable from the DO
host at `10.10.0.2`. On the DO host, relay public ports 80/443 to this
machine's Caddy over the tunnel:

```sh
apt-get install socat
socat TCP-LISTEN:80,fork,reuseaddr TCP:10.10.0.2:80 &
socat TCP-LISTEN:443,fork,reuseaddr TCP:10.10.0.2:443 &
```

For a persistent setup, wrap these in a systemd unit (one instance per
port, or a small template unit) rather than leaving them as background
jobs, e.g. `/etc/systemd/system/socat-http.service`:

```ini
[Unit]
Description=socat relay: public :80 -> tunnel -> this machine :80
After=network-online.target wg-quick@wg0.service
Requires=wg-quick@wg0.service

[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:80,fork,reuseaddr TCP:10.10.0.2:80
Restart=always

[Install]
WantedBy=multi-user.target
```

(Duplicate for port 443 with `TCP-LISTEN:443`.) Enable and start both with
`systemctl enable --now socat-http socat-https`.

### 3. Caddy's ACME/domain configuration

Point your domain's DNS `A`/`AAAA` record at the **DigitalOcean host's
public IP** (not this machine's — this machine has no public IP; the DO
host is the only internet-facing endpoint). `site_domain` in `prep.toml`
must match that DNS name exactly, since it's baked into the generated
Caddyfile's site block and `tls <acme_email>` directive.

Caddy needs to prove domain ownership to Let's Encrypt one of two ways:

- **HTTP-01 (default, simplest)**: Let's Encrypt connects to port 80 of
  your domain and expects Caddy to answer an ACME challenge there. This
  requires that the full path — DNS → DO host's public IP → `socat` on
  port 80 → WireGuard tunnel → this machine's Caddy container on port 80 —
  actually works end to end *before* you first bring Caddy up for real,
  since issuance happens automatically on Caddy's first HTTPS request for
  the domain. No extra configuration is needed in the Caddyfile itself for
  this path; it's `prep.caddyconfig.generate_caddyfile`'s default `tls
  <acme_email>` directive. This is what `Caddyfile.example` shows.
- **DNS-01 (alternative, if you don't want to expose port 80 through the
  tunnel)**: Caddy instead proves ownership by creating a TXT record via
  your DNS provider's API. This requires a DNS-provider-specific Caddy
  plugin (not in the stock `caddy:2.8.4-alpine` image used here — you'd
  need to build a custom Caddy image with that plugin compiled in, e.g.
  via `xcaddy`) and an API token for your DNS provider supplied as an
  environment variable or Caddy config secret. If you go this route,
  `docker-compose.yml`'s `caddy` service image and the generated
  Caddyfile's `tls` directive both need to change accordingly (this isn't
  automated by `prep` — hand-edit the generated Caddyfile's `tls` line, or
  extend `prep/caddyconfig.py`, if you adopt DNS-01). Prefer this path if
  you'd rather not route port 80 through the WireGuard tunnel at all.

Either way, port 443 still needs to reach Caddy for actual HTTPS traffic
after issuance — only the *challenge* mechanism differs between the two.

## Development / running tests

No host Python install is required. From the repo root:

```sh
docker compose run --rm prep pytest
```

This runs the full test suite (currently 173 passed inside the container —
3 of the tests that skip on a host without `ffmpeg`/`ffprobe` installed run
for real here) with zero host-side dependencies beyond Docker itself.

If you do have a local Python environment set up for development
(optional, not required for normal use), `pip install -e ".[dev]"` from the
repo root followed by `pytest` works too — expect `170 passed, 3 skipped`
on a host without `ffmpeg`/`ffprobe`.

## Repository layout

```
prep/                # Python package: the `prep` CLI
  cli.py             # click entrypoint: publish, sync-routes, hash-password,
                      #   users add/passwd/delete
  users.py           # users.yml load/save/add/update/remove, bcrypt
  manifest.py        # publish.yaml model, load/validate/save, discover_albums()
  metadata.py        # EXIF/ffprobe date extraction w/ mtime fallback
  layout.py          # day-grouping + irregular grid position algorithm
  assets.py          # thumbnail + video poster-frame generation
  render.py          # Jinja2 rendering: album/picture pages + albums index
  caddyconfig.py     # Caddyfile generation from users.yml + manifests
  Dockerfile         # containerizes prep (see Host prerequisites above)
  templates/         # Jinja2 HTML templates
  static/            # shared CSS/JS, synced into site/static/
tests/               # pytest suite + fixtures
users.yml            # created by `prep users add` (gitignored)
prep.toml            # deployment config (gitignored; copy from prep.example.toml)
site/                # generated output: top-level index.html, static/, .covers/
                      #   (gitignored; per-album content lives under ~/Pictures instead)
caddy/               # generated Caddyfile lives here (gitignored)
Caddyfile.example    # checked-in reference showing the generated shape
docker-compose.yml
pyproject.toml
```

Per-album content (`publish.yaml`, generated `index.html`, `picture/*.html`,
`.web/thumbs/`, `work/`) lives inside each album's own folder under
`~/Pictures`, outside this repo entirely.

## License

Licensed under the GNU General Public License v3.0 — see `LICENSE`. See
`CONTRIBUTING.md` for the contribution/rights-assignment terms that apply
to anything submitted to this repository.
