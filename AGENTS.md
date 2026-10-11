# AGENTS.md

## Commands

- **Install deps:** `uv sync`
- **Run tests:** `uv run pytest`
- **Run locally:** `BAYCONE_CONFIG=config.yml BAYCONE_LOCATIONS=/your/media uv run python -m baycone`
- **Docker build/run:** `docker compose build && docker compose up -d`
- **Format:** `black .` (line-length 120, target `py314`) — configured under `[tool.black]` in `pyproject.toml`, so
  never pass those as flags. Not a declared dev dependency: it resolves from PATH / a global `uv tool` install, not from
  the project venv, so `uv sync` alone does not provide it.
- **Bump version:** `uvx bump-my-version bump patch|minor|major` — configured under `[tool.bumpversion]` in
  `pyproject.toml`; preview with `--dry-run -v`. Not a declared dev dependency either
- **No linter or typecheck** command is configured

## Requirements

- Python 3.14
- `ffmpeg` and `ffprobe` on PATH (or inside Docker image)
- `uv` package manager

## Test status

`uv run pytest` is green (231 tests). No real `ffmpeg`/`ffprobe` process is ever spawned — `subprocess.run`
and `subprocess.Popen` are mocked.

`tests/conftest.py` holds the shared fixtures. `Constants.DIR_LOCATIONS` is read from the environment at import time, so
tests patch the **class attribute**, not the environment:

- `locations_root` — points `Constants.DIR_LOCATIONS` at `tmp_path`
- `location_config` — a `LocationConfig` registered in the global `config.locations` (required, or
  `FileInfo` raises `ValueError: No 'LocationConfig' found`)
- `location_dir` — the watched directory itself (`location_config.abspath`)

Two tests guard against past data-loss/logic bugs — keep them:

- `TestArchiveOriginal::test_keeps_directories_holding_unprocessed_files`
- `TestAnalyze::test_audio_codec_check_is_not_a_substring_match`

## Architecture

- `baycone/__main__.py` — Service entry point; `Service` class + `main_loop`. SIGTERM is turned into
  `KeyboardInterrupt` (PID 1 in a container ignores it otherwise), and a running ffmpeg is killed on interruption.
- `baycone/config.py` — **Module-level global singleton** (`config: Config = Config()`). `load_config()` mutates this
  in-place. Other modules import `from baycone.config import config` and read from it at runtime.
- `baycone/audio.py` — `AudioStream` (probed), `AudioTrack` (output) and `select_audio_tracks()`, a pure function
  picking the original language first, then `target_audio_languages`, one track per language by `target_audio_channels`
- `baycone/encoding.py` — `Analyzer` (ffprobe), `Encoder` (ffmpeg two-pass), `FileInfo` data class
- `baycone/ffmpeg.py` — CLI template lists with `{placeholder}` substitution (not f-strings). Do not hard-code paths in
  command lists. The binary itself is a placeholder too (`{ffmpeg}` / `{ffprobe}`), filled from `config.ffmpeg_path` /
  `config.ffprobe_path`. Variable-length sections are spliced in at the `AUDIO_ARGS` / `VIDEO_ARGS` elements by
  `_render()`, built from the `ARGS_*` and `FILTER_*` templates. Both encode passes must get the same video filters.
- `baycone/watcher.py` — `DirWatcher` polls via `Path.walk`, pruning `SpecialDirs` instead of descending into them (they
  grow to the size of the library). A file is ready once its size and mtime are unchanged since the previous
  scan — never sleep per file. Failed files are skipped until they change (`mark_failed`).
- `baycone/cli.py` — Standalone CLI entry point (separate from service mode), uses `argparse`. Imports
  `from baycone import ffmpeg` — there is no third-party `ffmpeg` dependency.

**`Encoder.process()` invariant:** the original file is moved to `_baycone_originals/` only after both encoding and
optimizing succeeded. On any failure it stays put, `process()` returns `False` and the watcher skips it until the file
changes — a failing file must never starve the queue. Only streams that need it are re-encoded: video is copied when it
is within limits, and a container mismatch alone is fixed by the optimize remux. Black-bar detection
(`Analyzer.detect_crop`) decodes keyframes of the whole file, so it only runs when the video is re-encoded anyway.
`_cleanup_workdirs()` always runs (`finally`).

**Directory pruning** (`_remove_empty_dirs`) walks up with `rmdir()`, which only ever succeeds on empty directories.
Never `rmtree` anything under the watch root — sibling media files may not be processed yet.

**Logging** is configured only by `setup_logging()` in `baycone/__main__.py` — importing a module must not configure
logging.

## Conventions

- Dataclasses for all structured data; `field(default_factory=...)` for mutable defaults
- `pathlib.Path` everywhere, never `os.path` strings
- `logging.getLogger(__name__)` at module level; never `print()` for diagnostics
- Type hints mandatory on all function signatures and class attributes
- Audio bitrate is **per-channel** (`target_audio_channel_bitrate × channels`)
- `Config → DefaultsConfig → LocationConfig` hierarchy; every location inherits defaults and can override
  (`Config.from_dict` seeds each location with `defaults.to_dict()` before applying its own keys)
- Audio languages are ISO 639-2, normalized to the /T code (`cze` → `ces`) by `normalize_language()` before comparing
- Special audio tracks (commentary, audio description, ...) are detected by ffprobe disposition only and always dropped;
  `dub` is not special
- `FileInfo.file_path` setter auto-resolves `LocationConfig` and `SpecialDirs` from the global `config.locations`
- Use `deepcopy`/`copy` when returning mutable internal state from properties
- Docstrings and comments only where the code can't speak for itself (a non-obvious why, invariant or gotcha) —
  one short line, no restating names or signatures, no section banners, no commented-out code. `TODO`s are fine

## Branching and releases

- `master` — trunk branch, holding working code and releases. Small changes, single-commit features and fixes go
  straight here.
- `feature/...` — larger changes. Prepend the issue number when an issue exists (`feature/42-multitrack-audio`,
  otherwise `feature/multitrack-audio`). Merged into `master` once complete.
- `support/v<major>.<minor>.x` — fixes for older releases (`support/v0.1.x`, `support/v1.1.x`).
- Releases are marked by tags: `v0.1.0`, `v1.0.0`.

Only git refs carry the `v` prefix — the same version is written differently per artifact:

| Artifact          | Form                         | Example                                       |
|-------------------|------------------------------|-----------------------------------------------|
| Git tag           | `v` + semver                 | `v0.1.0`                                      |
| `support/` branch | `support/v<major>.<minor>.x` | `support/v1.1.x`                              |
| Changelog heading | plain semver                 | `## [0.1.0]` — required by Common Changelog   |
| Docker image tag  | plain semver                 | `theobaldik/baycone:0.1.0`, `:0.1`, `:latest` |

`project.version` in `pyproject.toml` is the single source of truth. Never edit a version by hand — `bump-my-version`
rewrites it together with every copy listed under `[tool.bumpversion.files]` (`uv.lock`, `compose.yml`, the `image:`
lines in `README.md` and `docs/dockerhub-overview.md`). A new file that pins the release version must be added there.
It neither commits nor tags — update `CHANGELOG.md`, commit, then tag `v<version>` by hand.

Image tags are declared in `compose.yml` under `build.tags` — always fully qualified as `theobaldik/baycone:...`, never
a bare `baycone:...` (a bare name resolves to `docker.io/library/`, which cannot be pushed). The GitLab registry is deprecated and no longer published to.

`:latest` must only be pushed for a release from `master`. A patch cut from a `support/v<major>.<minor>.x` branch
publishes its exact and `major.minor` tags only; moving `:latest` to it would hand every `:latest` user an older
release.

Which branch a fix belongs on depends on the release that needs it:

- Latest `major.minor` → `master`
- Any older `major.minor` → `support/v<major>.<minor>.x`

For example, with `v1.2.0` released: a fix for `1.1.0` goes to `support/v1.1.x`, a fix for `1.2.x` goes to `master`.

## Changelog and commits

**Commits** follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):
`<type>[(scope)]: <description>`.

- Types: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`, `chore`, `style`
- Scope is the module without `.py` (`encoding`, `watcher`, `ffmpeg`, `config`, `cli`) or `docker` / `docs`
- Breaking changes take a `!` before the colon (`feat(config)!: ...`) and/or a `BREAKING CHANGE:` footer

**`CHANGELOG.md`** follows [Common Changelog](https://common-changelog.org/):

- Release heading `## [0.1.0] - YYYY-MM-DD` — semver **without** the `v` prefix (the tag keeps it), ISO 8601 date,
  version as a reference-style link to the matching tag:
  `[0.1.0]: https://github.com/<owner>/baycone/releases/tag/v0.1.0`
- Allowed groups, in exactly this order: `### Changed`, `### Added`, `### Removed`, `### Fixed`. Nothing else — no
  `Deprecated`, no `Security` (deprecations go under `Changed`), no `Notes`, and never an `Unreleased` section
- One line per entry, imperative present tense, self-describing without its heading ("Add `--optimize` flag", not
  "added optimize flag" or "optimize flag"), followed by a reference: ``- Add X ([`abc1234`](url))``
- Breaking changes are prefixed `**Breaking:**` and listed first within their group
- A release may open with a single one-sentence notice (`_Initial release._`)
- Never paste commit messages, PR titles, or `feat:`-style prefixes into the changelog — rewrite them for humans
- Limitations and known gaps belong in the README, not the changelog

## Docker

Multi-stage build: `uv` installs deps in builder stage, final image is `python:3.14-slim` + `ffmpeg` via `apt`. Always
use `uv sync --frozen --no-dev` in Docker — never upgrade silently.

Image tags use the plain version with no `v` prefix (`theobaldik/baycone:0.1.0`) — see the table under
[Branching and releases](#branching-and-releases).

- Both base images are pinned to a patch version **and** a multi-arch index digest. Bump them deliberately; never loosen
  a pin back to a floating tag like `:latest`.
- The `uv` pin must track the uv version that maintains `uv.lock` (currently `0.9.21`).
- The image runs as **uid/gid 1000, non-root** (`USER 1000:1000`). Anything the container writes goes into a
  bind-mounted library, so never add build steps that require root at runtime; hosts with different ids override
  `user:` rather than rebuilding.
- `.dockerignore` is a whitelist (`*` followed by re-includes). A new file the image needs must be opted in there
  explicitly — currently `pyproject.toml`, `uv.lock`, `baycone/`, `LICENSE`, `NOTICE`.
- `LICENSE` and `NOTICE` are copied to `/usr/share/doc/baycone/`: publishing the image distributes the software, so MIT
  attribution and the GPL notices for bundled Debian packages have to travel with the artifact, not just the repo.

## Out of scope

- `_backup/` directory — historical reference only, do not use
- No REST API, GUI, or web interface