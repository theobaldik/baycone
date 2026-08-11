# syntax=docker/dockerfile:1
# ── Build stage ──────────────────────────────────────────────────────────────
# Base images are pinned to a patch version *and* a multi-arch index digest, so
# a rebuild produces the same image on any host and architecture.
FROM python:3.14.7-slim-trixie@sha256:83c1cebb322d099ac9e3a3a532ba74b0146d702838b25e4c75c02fa81ffeb910 AS builder

# Install uv — pinned to the version that maintains uv.lock (see AGENTS.md)
COPY --from=ghcr.io/astral-sh/uv:0.9.21@sha256:15f68a476b768083505fe1dbfcc998344d0135f0ca1b8465c4760b323904f05a /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first (layer-cache friendly)
COPY pyproject.toml uv.lock ./

# Install production dependencies into a virtual environment
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself
COPY baycone/ ./baycone/
RUN uv sync --frozen --no-dev

# ── Final stage ───────────────────────────────────────────────────────────────
FROM python:3.14.7-slim-trixie@sha256:83c1cebb322d099ac9e3a3a532ba74b0146d702838b25e4c75c02fa81ffeb910

# `licenses` describes the whole image, not just Baycone: the bundled Debian
# ffmpeg is GPL. See /usr/share/doc/baycone/NOTICE inside the image.
LABEL org.opencontainers.image.title="Baycone" \
      org.opencontainers.image.description="Media pipeline for self-hosted libraries" \
      org.opencontainers.image.licenses="MIT AND GPL-2.0-or-later" \
      org.opencontainers.image.source="https://github.com/theobaldik/baycone" \
      org.opencontainers.image.documentation="https://github.com/theobaldik/baycone#readme"

# Install ffmpeg. ca-certificates is kept so ffmpeg can read https:// inputs —
# Baycone itself makes no network calls.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring in the virtual environment from the builder
COPY --from=builder /app/.venv ./.venv

# Bring in application source
COPY --from=builder /app/baycone ./baycone/

# Ship the license and the third-party notice with the artifact: distributing
# the image is distributing the software, so MIT's own attribution clause and
# the GPL's notice requirements apply to the image, not only to the repository.
COPY LICENSE NOTICE /usr/share/doc/baycone/

# Make the venv's Python the default interpreter.
# BAYCONE_LOCATIONS / BAYCONE_CONFIG are the canonical runtime knobs;
# the values below are the Docker-first defaults and can be overridden at
# `docker run -e` or in compose.yml → environment / env_file.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BAYCONE_LOCATIONS="/locations" \
    BAYCONE_CONFIG="/config/config.yml"

# Run as a non-root user: Baycone feeds untrusted media files to ffmpeg, and
# every file it writes lands in a bind-mounted library.
#
# UID/GID 1000 is the first regular user on most Linux hosts, so bind-mounted
# media stays writable out of the box. When the host ids differ, override them
# at runtime instead of rebuilding — the numeric USER below needs no matching
# passwd entry:
#   docker run --user "$(id -u):$(id -g)" ...
#   compose.yml → services.baycone.user: "1001:1001"
RUN groupadd --gid 1000 baycone && \
    useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin baycone
USER 1000:1000

ENTRYPOINT ["python", "-m", "baycone"]
