# Changelog

## [Unreleased]

### Changed

- Remember failed files across restarts and retry them once the file changes or the configuration is reloaded
- Reject configuration values of the wrong type when loading, instead of failing later during a run
- Restart the container automatically and rotate its logs in the Compose setup

### Added

- Resume an interrupted encode with its second pass when the first pass had finished
- Add `check_free_space`, on by default: pause a location while its disk lacks the space for the next file
- Stop an ffmpeg that makes no progress for 30 minutes and fail the file
- Skip a file after the service died three times in a row while processing it
- Keep an earlier original of the same name in `_baycone_originals/`, archiving the newer one under a numbered name

### Fixed

- Skip input files whose copy was cut off, instead of encoding the truncated file
- Move a finished file into `_baycone_processed/` in one step, so a crash or power loss never leaves a broken one there
- Discard the result when the source file changes or disappears while it is processed
- Keep the service running when a `config.override.yml` is not a mapping, not UTF-8 or holds a wrongly typed value
- Fix two-pass hevc encodes of files with a `:` in their name
- Stop a location nested in another one from processing that location's originals and outputs again
- Remove work files left behind by a crash when the service starts

## [0.5.1] - 2026-11-10

### Changed

- Harden the Compose setup: no network, read-only root filesystem, all capabilities dropped and no privilege
  escalation, limiting what a malicious media file could do through an ffmpeg vulnerability

### Removed

- Remove `pip` from the Docker image, which is unused at runtime and bundled vulnerable copies of `urllib3`, `msgpack`
  and `setuptools`

## [0.5.0] - 2026-11-10

### Changed

- Update the Docker image to Python 3.14.10

### Added

- Add `hevc` as `target_video_codec`, encoded by `libx265` in 10-bit Main10 and tagged `hvc1` for Apple players
- Keep 10-bit SDR HEVC sources as they are when `hevc` is in `allowed_video_codecs` and within the limits

### Fixed

- Let x264 choose the h264 level, so encodes above 1080p are no longer mislabeled as level 4.1
- Skip a file with an unsupported `target_video_codec` instead of silently encoding it to h264

## [0.4.0] - 2026-10-11

### Changed

- **Breaking:** Mount the config directory in Docker (`./config:/config:ro`); move `config.yml` into a `config/`
  directory
- Compare and cap source bitrates as their h264 or aac equivalent, so HEVC, AV1, VP9 and Opus sources keep their quality

### Added

- Reload `config.yml` automatically when it changes, keeping the previous configuration when the new one is invalid

## [0.3.0] - 2026-10-11

### Changed

- **Breaking:** Keep the original audio track only when `original` is listed in `target_audio_languages`; add it to
  custom lists, the default is now `[ original, eng, cze ]`
- Order audio tracks by `target_audio_languages`, so `original` can be placed anywhere in the list
- Keep the original audio track when none of the listed languages is found
- Resolve ISO 639-2/B audio language codes with `pycountry`

### Added

- Add `target_subtitle_languages` to extract text subtitles to external `.srt` files next to the processed file
- Name extracted subtitles with the two-letter language code, e.g. `mymovie.cs.srt`
- Convert extracted subtitles in a legacy codepage to UTF-8

## [0.2.0] - 2026-10-11

### Changed

- Copy the video stream when only the audio needs re-encoding
- Retry a failed file only once it changes

### Added

- Add multitrack audio: the original language plus `target_audio_languages`, one track per language
- Add support for 4K, 10-bit and HDR sources, with black bar cropping
- Add progress and ETA to the log

### Fixed

- Stop promptly on `docker stop`
- Process videos without an audio track
- Drop cover art from the output

## [0.1.0] - 2026-08-11

_Initial release._

### Added

- Add service that watches multiple directories and transcodes new video files for Jellyfin
- Add two-pass `libx264` encode to h264 / AAC MP4 with `+faststart`, metadata stripped and the audio track tagged with
  its language and channel layout
- Add codec and bitrate checks that remux files already matching the target container, codecs and bitrates instead of
  re-encoding them
- Add per-location configuration inheriting from `defaults`, plus a runtime `.baycone/config.override.yml` per location
- Add configurable `ffmpeg_path` / `ffprobe_path`
- Add lock detection so files that are still being copied in are picked up only once they are stable
- Add standalone CLI (`bin/baycone`) with `--optimize`, `--info` and progress output
- Add Docker image (multi-stage `uv` build on `python:3.14-slim` with `ffmpeg`)
- Add PlantUML architecture and behavior diagrams in `docs/`
- Add MIT license

[0.5.1]: https://github.com/theobaldik/baycone/releases/tag/v0.5.1
[0.5.0]: https://github.com/theobaldik/baycone/releases/tag/v0.5.0
[0.4.0]: https://github.com/theobaldik/baycone/releases/tag/v0.4.0
[0.3.0]: https://github.com/theobaldik/baycone/releases/tag/v0.3.0
[0.2.0]: https://github.com/theobaldik/baycone/releases/tag/v0.2.0
[0.1.0]: https://github.com/theobaldik/baycone/releases/tag/v0.1.0
