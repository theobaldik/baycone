# Baycone

**Media pipeline for self-hosted libraries**

Baycone watches one or more media directories for new video files and automatically transcodes them with
`ffmpeg` into a streaming-friendly MP4 — h264 / AAC with the moov atom up front. Built for Jellyfin, but the output is
plain MP4 that any player handles.

## Features

- Watches multiple directories for new video files (configurable poll interval)
- Transcodes to **h264 / AAC** MP4 using a two-pass `libx264` encode, always 8-bit (`yuv420p`) for broad player
  support
- Scales larger videos down to fit 1920×1080 (configurable), crops black bars at the top and bottom, and tone maps
  HDR10 / HLG to SDR — all only when the video is re-encoded anyway
- Moves the moov atom to the front of the file (`-movflags +faststart`) for instant streaming
- Keeps the original audio language plus your preferred languages, one track per language at the preferred channel
  count, and drops the rest
- Strips all metadata and cover art, and tags every audio track with its language and channel layout
- Skips files that already meet the target codec / container / bitrate requirements, only remuxes files whose
  container alone differs, and copies the video stream when only the audio needs re-encoding
- Waits for files that are still being copied in, and never touches the original until the encoded result has been
  written successfully. A file that fails is left in place and retried once it is modified
- Runs as a Docker container; configured entirely through a YAML file and environment variables
- Ships a standalone CLI (`bin/baycone`) for one-off transcodes

## Limitations

- Commentary, audio description and other special audio tracks are always dropped. They are recognised by their
  disposition flags only, so an unflagged one can still be picked as a regular track of its language.
- MP4 has no "original" track flag — the original language is marked by being the first (default) track. The flag is
  kept when `target_container` is Matroska.
- The standalone CLI handles the first audio track only.
- Subtitles are always dropped from the output.
- A file whose bitrate is not reported is always re-encoded, even when it would be within the limits.
- Dolby Vision profile 5 (no HDR10 base layer) is not supported and comes out with wrong colours. Profile 7 and 8
  carry an HDR10 base layer and are tone mapped from that.

## Directory structure

Each watched root directory gets the following subdirectories created automatically:

```
videos/
├── .baycone/
│   ├── passlogfiles/       ← two-pass log files (temporary)
│   └── encoded/            ← intermediate encoded files (temporary)
├── _baycone_originals/   ← original files are moved here after encoding
└── _baycone_processed/   ← finished MP4s are moved here
```

## Configuration

Copy `config.yml.example` to `config.yml` and edit it. The minimal config is:

```yaml
locations:
  - path: movies         # relative to BAYCONE_LOCATIONS
  - path: tv-shows
```

All fields except the `path` in `locations` are optional and fall back to the built-in values shown below:

```yaml
ffmpeg_path: ffmpeg      # bare names are looked up on PATH
ffprobe_path: ffprobe
poll_interval: 5.0       # seconds between directory scans

defaults:
  allowed_extensions: [ .mp4, .mkv, .avi, .m4v, .mov, .webm ]

  # A file is left as-is when all of these already hold.
  allowed_containers: [ mp4 ]
  allowed_video_codecs: [ h264 ]
  allowed_audio_codecs: [ aac ]
  limit_video_bitrate: 2800         # kbps — files below this are not re-encoded
  limit_video_height: 1080          # larger videos are scaled down to fit 1920x1080
  limit_audio_channel_bitrate: 96   # kbps per channel

  # Used when a file does have to be re-encoded.
  target_extension: .mp4
  target_container: mp4
  target_video_codec: h264
  target_video_bitrate: 2250        # kbps — never upscales above the source bitrate
  target_audio_codec: aac
  target_audio_channel_bitrate: 64  # kbps per channel
  target_audio_languages: [ eng, cze ]  # kept after the original language, in this order
  target_audio_channels: 2          # preferred channel count per language

locations:
  - path: movies
  - path: tv-shows
    target_video_bitrate: 1800        # location-level overrides are supported
```

Each location inherits from `defaults` and can override any field individually. See
[`config.yml.example`](config.yml.example) for a fully commented file.

### Audio tracks

Every other audio track is dropped, so the output holds:

1. The **original language** — the track flagged `original`, otherwise the first audio track
2. One track for each language in `target_audio_languages` that the file has, in that order

When a language has several tracks, the one with `target_audio_channels` channels wins; without an exact match, the
fewest channels above it, otherwise the most channels below it. Tracks flagged as commentary, audio description,
hearing impaired, karaoke or lyrics are never kept. The first track is the default one, and each track is tagged with
its language and channel layout ("Stereo", "Surround 5.1", ...). Tracks within the limits are copied, the rest are
re-encoded. A video without any audio track is processed into a video-only file.

A location can also be reconfigured at runtime without restarting the service by dropping a
`.baycone/config.override.yml` file into it; it is re-read whenever it changes and applies to that location only.
Removing it restores the location's settings from `config.yml`.

## Environment variables

| Variable            | Default (Docker)     | Description                                |
|---------------------|----------------------|--------------------------------------------|
| `BAYCONE_CONFIG`    | `/config/config.yml` | Path to the YAML config file               |
| `BAYCONE_LOCATIONS` | `/locations`         | Base directory for relative location paths |

There is usually no need to override the variables when running in Docker.

## Running with Docker Compose

```yaml
# compose.yml
services:
  baycone:
    image: theobaldik/baycone:0.1.0
    user: "1000:1000"
    environment:
      BAYCONE_CONFIG: /config/config.yml
      BAYCONE_LOCATIONS: /locations
    volumes:
      - ./config.yml:/config/config.yml:ro
      - /your/movies:/locations/movies
      - /your/tv-shows:/locations/tv-shows
```

```bash
docker compose up -d
docker compose logs -f baycone   # follow what the service is doing
```

Each file logs what will happen to it, then its progress in 10 % steps with an estimate of the remaining time:

```
Processing 'Show/Season 1/episode.mkv' (0:42:10)
Video: hevc 5200 kbps, encode to h264 2250 kbps
Audio: eng Surround 5.1 (ac3 -> aac 384 kbps), ces Stereo (aac, copy); 2 track(s) dropped
'Show/Season 1/episode.mkv' Encoding 1st pass: 40 %, ETA 0:07:12
```

Published tags use the plain version — `theobaldik/baycone:0.1.0`, `:0.1`, `:latest`.

The container runs as a non-root user, uid/gid **1000** by default, so it needs read and write access to the mounted
directories. Point `user:` at whichever host account owns your media if that isn't uid 1000:

```bash
id -u; id -g        # host ids to use
```

Keep machine-specific paths in a `compose.override.yml` — Compose merges it automatically and it is git-ignored.

## Building the image

```bash
docker compose build
```

The Dockerfile uses a multi-stage build: `uv` installs dependencies in a builder stage, and only the virtual environment
and source are copied into the final `python:3.14-slim` image alongside `ffmpeg`.

## Running locally (without Docker)

Requires Python 3.14, `ffmpeg`, and `ffprobe` on `PATH`.

```bash
uv sync
BAYCONE_CONFIG=config.yml BAYCONE_LOCATIONS=/your/media uv run python -m baycone
```

## Standalone CLI

For transcoding a single file without running the service:

```bash
bin/baycone movie.mkv                 # two-pass encode, then optimize
bin/baycone movie.mkv -o              # skip the encode, only optimize
bin/baycone movie.mkv -i              # print the ffprobe result and exit
bin/baycone movie.mkv -l cze -v 1800  # audio language and video bitrate
```

The result is written to `_baycone_processed/` next to the input file; the input itself is left untouched. Duration and
channel count are detected with `ffprobe`.

| Flag                    | Default | Description                         |
|-------------------------|---------|-------------------------------------|
| `-l`, `--language`      | `eng`   | Audio track language (3 characters) |
| `-v`, `--video-bitrate` | `2250`  | Target video bitrate in kbps        |
| `-a`, `--audio-bitrate` | `64`    | Target audio bitrate per channel    |
| `-o`, `--optimize`      | off     | Skip transcoding, only optimize     |
| `-i`, `--info`          | off     | Print file information and exit     |

## Running tests

```bash
uv run pytest
```

## Documentation

Architecture and behaviour diagrams live in [`docs/`](docs) as PlantUML sources with rendered SVGs:

| Diagram                                               | Shows                                    |
|-------------------------------------------------------|------------------------------------------|
| [`class_structure`](docs/class_structure.svg)         | Classes and their relations              |
| [`processing_activity`](docs/processing_activity.svg) | End-to-end flow for a single file        |
| [`processing_sequence`](docs/processing_sequence.svg) | Service ↔ watcher ↔ encoder polling loop |
| [`encoding_sequence`](docs/encoding_sequence.svg)     | Analyze → encode → optimize → archive    |

Regenerate them after editing a `.puml` with `plantuml -tsvg -nometadata docs/*.puml`.

## Contributing

Code is formatted with [Black](https://black.readthedocs.io/) — line length **120**, target version **`py314`** — as
configured under `[tool.black]` in [`pyproject.toml`](pyproject.toml). Run it over the project before committing; no
other flags are needed, since the settings come from the config file:

```bash
black .
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), and
[`CHANGELOG.md`](CHANGELOG.md) follows [Common Changelog](https://common-changelog.org/).

`master` is the trunk branch — small changes, single-commit features and fixes land there directly, while larger changes
go through a `feature/...` branch (prepended with the issue number where one exists) and are merged back once complete.
Releases are tagged `v0.1.0` / `v1.0.0`, and fixes for a release older than the latest `major.minor` go on a
`support/v1.1.x`-style branch instead of `master`. The `v` prefix belongs to git refs only — Docker image tags use the
plain version (`theobaldik/baycone:0.1.0`, `:0.1`, `:latest`).

The version lives in [`pyproject.toml`](pyproject.toml) and is bumped everywhere it is repeated (lock file, compose
image tags, docs) with [bump-my-version](https://github.com/callowayproject/bump-my-version):

```bash
uvx bump-my-version bump patch   # or minor / major; add --dry-run -v to preview
```

See [`AGENTS.md`](AGENTS.md) for the full set of project conventions.

## License

Baycone is released under the [MIT License](LICENSE).

### Third-party components

Baycone's Python dependencies are colorlog (MIT) and PyYAML (MIT). They are installed from PyPI and no third-party code
is vendored into this repository.

The published Docker image additionally bundles system packages that keep their own licenses — most notably `ffmpeg`,
which Debian builds with `--enable-gpl` (libx264 / libx265) and distributes under the GPL. Baycone runs `ffmpeg` and
`ffprobe` as external processes and does not link against them, so the MIT license covers Baycone's own source only; the
image as a whole is a collection of separately licensed works.

The bundled packages are unmodified Debian packages. Their license texts are kept inside the image under
`/usr/share/doc/<package>/copyright`, the summary below travels with the image as
[`NOTICE`](NOTICE) at `/usr/share/doc/baycone/NOTICE`, and the source is available from Debian via
[sources.debian.org](https://sources.debian.org) or:

```bash
apt-get source ffmpeg
```
