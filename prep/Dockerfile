# Containerizes the `prep` CLI so none of its dependencies (Pillow, piexif,
# ffmpeg/ffprobe, bcrypt, etc.) are ever installed on the host. Built locally
# via `docker compose build prep` (see Task 17's docker-compose.yml) — never
# published to any registry, since this is a personal tool. Not part of
# `docker compose up`: invoked ad hoc only via `docker compose run --rm prep
# <args>`.
#
# Build context must be the project repo root (not this `prep/` directory),
# e.g.:
#   docker build -f prep/Dockerfile -t album-prep .
# so that this Dockerfile can see the top-level `pyproject.toml` and
# `tests/` alongside the `prep` package itself. Task 17's docker-compose.yml
# sets `build.context: .` with `build.dockerfile: prep/Dockerfile`
# accordingly.
FROM python:3.12-slim

# ffmpeg also provides ffprobe, used by prep.metadata for video date
# extraction and by prep.assets for poster-frame generation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY prep ./prep
COPY tests ./tests

# Editable install: the `prep` package is already copied in full above, and
# an editable install avoids a redundant second copy into site-packages.
# The `dev` extra pulls in pytest, so `docker compose run --rm prep pytest`
# works with no separate test image/stage.
RUN pip install --no-cache-dir -e ".[dev]"

ENTRYPOINT ["prep"]
