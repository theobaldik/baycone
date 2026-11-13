# AGENTS.md

## Commands

- **Install deps:** `uv sync`
- **Run tests:** `uv run pytest`
- **Run locally:** `BAYCONE_CONFIG=config/config.yml BAYCONE_LOCATIONS=/your/media uv run baycone-service`
- **Run the CLI:** `uv run baycone-cli movie.mkv` (or `bin/baycone`)
- **Build the wheel:** `uv build` — one distribution holding all three packages, built by `uv_build`
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

`uv run pytest` is green on Linux (474 tests). On Windows three tests fail for platform reasons (CRLF, path
separators, `os.fsync` mocking). No real `ffmpeg`/`ffprobe` process is ever spawned — `subprocess.run` and
`subprocess.Popen` are mocked.

Tests mirror the packages: `tests/core/`, `tests/service/`, `tests/cli/`.

- `tests/core/test_encoding.py` has an autouse `settings` fixture: every `Encoder()` / `Analyzer()` a test creates
  shares that one `EncodingSettings`, so a test can change it at any point. `job_for(path)` builds a `Job` and
  `encoder_for(file_info)` an encoder mid-process.
- `tests/service/conftest.py` holds the service fixtures. `Constants.DIR_LOCATIONS` is read from the environment at
  import time, so tests patch the **class attribute**, not the environment:
  - `locations_root` — points `Constants.DIR_LOCATIONS` at `tmp_path`
  - `location_config` — a `LocationConfig` registered in the global `config.locations` (required, or
    `LocatedFile.resolve` raises `ValueError: No 'LocationConfig' found`)
  - `location_dir` — the watched directory itself (`location_config.abspath`)

Two tests guard against past data-loss/logic bugs — keep them:

- `TestArchiveOriginal::test_keeps_directories_holding_unprocessed_files`
- `TestAnalyze::test_audio_codec_check_is_not_a_substring_match`

## Architecture

Three import packages in one distribution. `baycone_cli` and `baycone_service` depend on `baycone`, never the other
way, and the service never on the CLI. A later split into a uv workspace is a folder move.

**`baycone` — core library.** No global state, no environment variables, no YAML, no logging configuration.

- `baycone/__init__.py` — the public API: `Encoder`, `Analyzer`, `Job`, `FileInfo`, `EncodingSettings`, `FFmpegTools`
  and the errors
- `baycone/settings.py` — `EncodingSettings` (every per-file setting, the `defaults` section of `config.yml` minus
  `allowed_extensions`), `FFmpegTools` (binary paths) and `Serializable` / `_validate`, which reject a value of the
  wrong type at load rather than mid-run
- `baycone/errors.py` — `BayconeError` and its subclasses `InsufficientSpaceError`, `InvalidFileError`,
  `EncodingError`, `SourceChangedError`
- `baycone/encoding.py` — `Analyzer` (ffprobe), `Encoder` (ffmpeg two-pass), `FileInfo` (the analysis) and `Job`
  (input, output, work dir and the work paths derived from them). Both take settings and tools in the constructor and
  deep-copy the settings. A file is invalid when no packet lies near its declared end (`Analyzer._is_complete`, one
  seek, no decode): an aborted copy keeps the full-length header, and its size and mtime are as stable as a finished
  one's. It reads every packet from the seek position on and takes the latest pts *or* dts — AVI has no pts, and the
  seek lands on the keyframe before its target, so the first packet alone says nothing
- `baycone/audio.py` — `AudioStream` (probed), `AudioTrack` (output) and `select_audio_tracks()`, a pure function
  picking `target_audio_languages` in order (`original` stands for the original track's language, which is kept when
  nothing matches), one track per language by `target_audio_channels`
- `baycone/subtitles.py` — `SubtitleStream` and `select_subtitle_streams()` (text codecs only, `target_subtitle_languages`
  in order), `language_suffix()` (`ces` → `cs`) and `ensure_utf8()` (charset detection via `charset-normalizer`).
  Extracted by `Encoder._extract_subtitles()` after publishing; a failure there only warns
- `baycone/languages.py` — the ISO 639 tables behind `normalize_language()` and `language_suffix()`;
  `tests/core/test_languages.py` checks them against `pycountry`, a dev dependency only
- `baycone/ffmpeg.py` — CLI template lists with `{placeholder}` substitution (not f-strings). Do not hard-code paths in
  command lists. The binary itself is a placeholder too (`{ffmpeg}` / `{ffprobe}`), filled from the `tools` argument
  every `get_cmd_*` takes. Variable-length sections are spliced in at the `AUDIO_ARGS` / `VIDEO_ARGS` /
  `VIDEO_CODEC_ARGS` elements by `_render()`, built from the `ARGS_*` and `FILTER_*` templates. Both encode passes
  must get the same video filters. Everything codec-specific (encoder, pixel formats, per-pass args) lives in one
  `VideoCodec` entry of `VIDEO_CODECS`, keyed by `target_video_codec`; x265 needs the same GOP and rate-control
  params in both passes, and `-x265-params` values are `key=value` with no `:` inside a value
- `baycone/files.py` — `file_signature()` (size + mtime, the one notion of "this file changed"), `fsync()` /
  `fsync_dir()` and `write_atomic()`. Anything that must survive a power loss goes through these. Windows only
  flushes a handle opened for writing, so `fsync()` opens files read-write there

**`baycone_service` — the watching service.**

- `baycone_service/service.py` — `Service` class + `main_loop`, `main()` for the `baycone-service` script and
  `python -m baycone_service` (the Docker `ENTRYPOINT`). SIGTERM is turned into `KeyboardInterrupt` (PID 1 in a
  container ignores it otherwise), and a running ffmpeg is killed on interruption. `config.yml` is hot-reloaded at the
  top of each `main_loop` iteration (`FileChangeTracker` mtime check, the same one `DirWatcher` uses for
  `config.override.yml`); a reload rebuilds the watchers, a failed one keeps the previous config. Docker mounts the
  config **directory** — a single-file bind mount misses editors' write-and-rename saves. `_process_file()` builds an
  `Encoder` per file from the location's settings and archives the original after a successful `process()`
- `baycone_service/config.py` — **Module-level global singleton** (`config: Config = Config()`). `load_config()`
  replaces its fields in place (removed keys fall back to defaults, a failed load leaves it untouched, an empty file is
  ignored). Other service modules import `from baycone_service.config import config` and read from it at runtime.
  `DefaultsConfig` extends `EncodingSettings` with `allowed_extensions`, `LocationConfig` adds `path`;
  `Config.tools()` gives the `FFmpegTools`. `Constants` and `SpecialDirs` (the location layout) live here too
- `baycone_service/layout.py` — `LocatedFile.resolve()` finds a file's `LocationConfig`, derives its processed and
  original paths and its `Job` (work dir `.baycone/`, the same `passlogfiles` / `encoded` / `optimized` folders as
  `SpecialDirs`). `archive_original()` moves the original to `_baycone_originals/`
- `baycone_service/state.py` — `State`, a location's `.baycone/state.json`: failed files and the file in progress with
  its attempt count. Written atomically; an unreadable file starts empty, a failed save only warns
- `baycone_service/watcher.py` — `DirWatcher` polls via `Path.walk`, pruning `SpecialDirs` instead of descending into
  them (they grow to the size of the library). A file is ready once its size and mtime are unchanged since the previous
  scan — never sleep per file. The special directory *names* are pruned at any depth, so nested locations do not
  process each other's outputs. `get_new_file()` opens an attempt and `finish(file, failed)` closes it — always pair
  them (`Service._process_next` does so in a `finally`, which a stop signal reaches too). An attempt left open means
  the service died; after `MAX_ATTEMPTS` of those the file counts as failed. Failed files are persisted and skipped
  until they change or `retry_failed()` is called (config or override reload). `pause()` holds a location back for
  `PAUSE_SECONDS` without failing anything.

**`baycone_cli` — the `baycone-cli` command.** `argparse` flags become `EncodingSettings` and a `Job`, then the core
`Encoder` runs. Its default work dir is `.baycone/cli/` next to the input and only that is removed:
`.baycone/` itself may hold a running service's state, override and work files. Progress goes to a single `\r` line on
stderr through `on_progress`; `ConsoleHandler` ends that line before any log record.

**`Encoder.process()` invariant:** it never touches the input file and returns `job.output_file`, or raises a
`BayconeError` with no output left behind. The service archives the original only after `process()` returned; on any
failure the original stays put, `_process_file()` returns `False` and the watcher skips the file until it changes — a
failing file must never starve the queue. Only streams that need it are re-encoded: video is copied when it is within
limits, and a container mismatch alone is fixed by the optimize remux; `remux_only=True` copies every stream. Black-bar
detection (`Analyzer.detect_crop`) decodes keyframes of the whole file, so it only runs when the video is re-encoded
anyway. `_cleanup_workdirs()` always runs (`finally`) and must not raise.

**Crash safety.** `_optimize()` writes into `<work dir>/optimized/`; `_publish()` fsyncs that file, renames it to
`job.output_file` and fsyncs the directory — the output path never holds a half-written file, and the service archives
the original only for an output that is durably on disk. Across filesystems (`EXDEV`) `_publish()` copies into a
hidden temp file next to the output, fsyncs it and renames it. Keep the order encode → optimize → source unchanged? →
publish → subtitles, then archive in the service. The source's signature is taken before the analysis and compared
before publishing; a changed or deleted source discards the result. `archive_original()` never overwrites
(`_free_path`). Everything under `.baycone/` except `state.json` and `config.override.yml` is disposable:
`Job.clear_workdirs()` runs after every file and `SpecialDirs.clear_workdirs()` when a `DirWatcher` is created
(startup, config reload — never during an encode).

**First-pass resume.** The passlog is *not* part of `clear_workdirs()`. `_first_pass()` fsyncs the stats and then
writes `passlogfiles/pass1.json` (source signature + the exact first-pass command); `_encode()` skips the first pass
when that matches and clears the passlog only before a fresh first pass and after a successful second one. A second
pass that fails on a kept first pass starts over once. The passlog file name is fixed (`passlog`), never derived
from the media file name.

**Things that wait instead of failing.** `Encoder.process()` raises `InsufficientSpaceError` before writing anything
(`check_free_space` on the work dir, skipped when the filesystem reports no total); the service pauses the location. Every ffmpeg
run is watched by `_kill_when_stalled()`: alive means `out_time_us` or the output file's signature moved within
`STALL_SECONDS` — the output file matters because faststart rewrites it while ffmpeg reports nothing.
`Service.main_loop` catches every `Exception` per iteration; a config value of the wrong type is rejected at load
(`_validate` in `baycone/settings.py`) rather than left to blow up mid-run.

**Directory pruning** (`_remove_empty_dirs`) walks up with `rmdir()`, which only ever succeeds on empty directories.
Never `rmtree` anything under the watch root — sibling media files may not be processed yet.

**Logging** is configured only by `setup_logging()` in `baycone_service/logger.py` (called from `main()`) and by the
CLI's `main()` — importing a module must not configure logging. The core logs plan and step results at INFO and the
10 % progress lines at DEBUG.

## Conventions

- Dataclasses for all structured data; `field(default_factory=...)` for mutable defaults
- `pathlib.Path` everywhere, never `os.path` strings
- `logging.getLogger(__name__)` at module level; never `print()` for diagnostics
- Type hints mandatory on all function signatures and class attributes
- Audio bitrate is **per-channel** (`target_audio_channel_bitrate × channels`)
- Source bitrates are converted to their h264 / aac equivalent (`H264_EFFICIENCY`, `AAC_EFFICIENCY`, built in, not
  configurable) before any limit check or cap; an unknown bitrate stays 0
- `Config → DefaultsConfig → LocationConfig` hierarchy; every location inherits defaults and can override
  (`Config.from_dict` seeds each location with `defaults.to_dict()` before applying its own keys)
- Audio languages are ISO 639-2, normalized to the /T code (`cze` → `ces`) by `normalize_language()` (backed by
  the built-in tables in `baycone/languages.py`; `pycountry` is a dev dependency that only verifies them) before
  comparing
- Special audio tracks (commentary, audio description, ...) are detected by ffprobe disposition only and always dropped;
  `dub` is not special
- `LocatedFile.resolve()` finds `LocationConfig` and `SpecialDirs` from the global `config.locations`; the core never
  looks at it
- Use `deepcopy`/`copy` when returning mutable internal state from properties
- American English (`color`, `behavior`, `recognize`, `mislabeled`) in code, comments, docs and the changelog.
  Third-party names keep their own spelling (x264's `analyse=`)
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
It neither commits nor tags — rename `## [Unreleased]` in `CHANGELOG.md` to the version, commit, then tag
`v<version>` by hand.

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
  `Deprecated`, no `Security` (deprecations go under `Changed`), no `Notes`
- Work that is not released yet is collected under `## [Unreleased]` at the top (a deliberate departure from Common
  Changelog); at release the heading becomes the version and date
- One line per entry, imperative present tense, self-describing without its heading ("Add `--optimize` flag", not
  "added optimize flag" or "optimize flag"), short and without commit references
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
- The `uv` pin must track the uv version that maintains `uv.lock` (currently `0.13.7`).
- The image runs as **uid/gid 1000, non-root** (`USER 1000:1000`). Anything the container writes goes into a
  bind-mounted library, so never add build steps that require root at runtime; hosts with different ids override
  `user:` rather than rebuilding.
- `.dockerignore` is a whitelist (`*` followed by re-includes). A new file the image needs must be opted in there
  explicitly — currently `pyproject.toml`, `uv.lock`, `README.md`, `baycone/`, `baycone_cli/`,
  `baycone_service/`, `LICENSE`, `NOTICE`. The build backend needs `README.md` and `LICENSE` for the package metadata.
- `LICENSE` and `NOTICE` are copied to `/usr/share/doc/baycone/`: publishing the image distributes the software, so MIT
  attribution and the GPL notices for bundled Debian packages have to travel with the artifact, not just the repo.

## Out of scope

- `_backup/` directory — historical reference only, do not use
- No REST API, GUI, or web interface