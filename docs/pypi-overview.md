# Baycone

**Transcode videos into streaming-friendly MP4 with ffmpeg — as a Python library or a command.**

Baycone turns a video file into an MP4 that plays everywhere and starts instantly: h264 or hevc video, AAC audio and
the moov atom up front. It only re-encodes what needs it, picks the audio tracks you want and never leaves a
half-written file behind.

This package is the pipeline behind the [Baycone Docker image](https://hub.docker.com/r/theobaldik/baycone), which
watches media libraries and processes new files automatically.

## Features

- Two-pass `libx264` / `libx265` encode to a target bitrate, never above the source's
- Copies streams that are already within limits; a container mismatch alone is fixed by a remux
- Scales down to fit 1920×1080 (configurable), crops black bars and tone maps HDR10 / HLG to SDR
- Keeps the original audio language plus your preferred ones, one track per language at the preferred channel count
- Extracts text subtitles to UTF-8 `.srt` files
- Detects copies that were cut off, kills a hung ffmpeg, and resumes an interrupted encode from its second pass
- Writes the output in one step after it is safely on disk; the input file is never modified

## Requirements

- Python 3.14
- `ffmpeg` and `ffprobe` on `PATH`, built with `libx264`, `libx265` and `libzimg` (most distribution builds are)

## Installation

```bash
pip install baycone
# or, for the command only
uv tool install baycone
```

## Command line

```bash
baycone-cli movie.mkv                          # result in _baycone_processed/movie.mp4 next to the input
baycone-cli movie.mkv --output /media/out.mp4
baycone-cli movie.mkv -l cze -l eng -v 1800    # audio languages in order, video bitrate in kbps
baycone-cli movie.mkv --codec hevc
baycone-cli movie.mkv --remux-only             # never encode, only remux into MP4
baycone-cli movie.mkv --info                   # print the analysis as JSON and exit
```

| Flag                    | Default                | Description                                             |
|-------------------------|------------------------|---------------------------------------------------------|
| `--output`              | `_baycone_processed/`  | Output file                                             |
| `--work-dir`            | `.baycone/cli/`        | Temporary files; the default one is removed afterwards  |
| `-l`, `--language`      | `original` `eng` `cze` | Audio language to keep, repeatable, in order            |
| `-v`, `--video-bitrate` | `2250`                 | Target video bitrate in kbps                            |
| `-a`, `--audio-bitrate` | `64`                   | Target audio bitrate per channel in kbps                |
| `--codec`               | `h264`                 | Target video codec, `h264` or `hevc`                    |
| `--subtitles`           | none                   | Subtitle language to extract to `.srt`, repeatable      |
| `--remux-only`          | off                    | Copy every stream, only remux into the target container |
| `--info`                | off                    | Print the analysis as JSON and exit                     |
| `--ffmpeg`, `--ffprobe` | `ffmpeg`, `ffprobe`    | Binaries to use                                         |

Exit codes: `0` success, `1` invalid arguments, `2` the file could not be processed.

## Library

```python
from pathlib import Path

from baycone import BayconeError, Encoder, EncodingSettings, Job

settings = EncodingSettings(target_video_bitrate=1800, target_audio_languages=["original", "eng"])
job = Job(input_file=Path("movie.mkv"), output_file=Path("out/movie.mp4"), work_dir=Path("work"))

try:
    Encoder(settings, on_progress=lambda step, percent: print(step, percent)).process(job)
except BayconeError as e:
    ...  # the input is untouched and no output is left behind
```

- `EncodingSettings` holds every encoding setting: allowed codecs and containers, targets and limits.
- `Encoder.process()` returns the output path or raises a `BayconeError`: `InsufficientSpaceError` before anything is
  written, `InvalidFileError` for an unreadable or truncated file, `EncodingError` when ffmpeg fails, and
  `SourceChangedError` when the input changed while it was processed.
- Keep `work_dir` on the same filesystem as `output_file`, otherwise publishing has to copy the file.
- `Analyzer(settings).analyze(path)` returns the analysis alone, without encoding anything.
- The library logs through `logging` under `baycone.*` and configures no handlers. It is fully typed.

## Links

- [Source code and documentation](https://github.com/theobaldik/baycone)
- [Changelog](https://github.com/theobaldik/baycone/blob/master/CHANGELOG.md)
- [Issues](https://github.com/theobaldik/baycone/issues)
- [Docker image](https://hub.docker.com/r/theobaldik/baycone)

Released under the [MIT License](https://github.com/theobaldik/baycone/blob/master/LICENSE).
