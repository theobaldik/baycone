# Changelog

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
- Add PlantUML architecture and behaviour diagrams in `docs/`
- Add MIT license

[0.2.0]: https://github.com/theobaldik/baycone/releases/tag/v0.2.0
[0.1.0]: https://github.com/theobaldik/baycone/releases/tag/v0.1.0
