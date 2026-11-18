import argparse
import json
import logging
import os
import re
import shutil
import signal
import sys
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from functools import partial
from pathlib import Path
from types import FrameType
from typing import NoReturn

from baycone import ffmpeg
from baycone.encoding import Analyzer, Encoder, Job
from baycone.errors import BayconeError
from baycone.settings import EncodingSettings, FFmpegTools

log = logging.getLogger(__name__)

# A directory of the CLI's own: `.baycone` itself may hold a running service's state and work files
DIR_WORK = Path(".baycone") / "cli"
DIR_OUTPUT = Path("_baycone_processed")
TIMESTAMP_PATTERN = re.compile(r"(?:(?:(\d+):)?(\d+):)?(\d+(?:\.\d+)?)")

EXIT_FAILED = 1
# What argparse exits with on an invalid argument
EXIT_USAGE = 2
# 128 + SIGINT, what shells report for a command stopped by Ctrl+C
EXIT_INTERRUPTED = 130


class Console:
    """One progress line on stderr; a log record first ends it, so the two never share a line."""

    def __init__(self) -> None:
        self._open: bool = False

    def progress(self, step: str, percent: float) -> None:
        sys.stderr.write(f"\r{step}: {percent:5.1f} %")
        sys.stderr.flush()
        self._open = True

    def end_line(self) -> None:
        if self._open:
            sys.stderr.write("\n")
            self._open = False


class ConsoleHandler(logging.StreamHandler):
    def __init__(self, console: Console) -> None:
        super().__init__(sys.stderr)
        self._console: Console = console

    def emit(self, record: logging.LogRecord) -> None:
        self._console.end_line()
        super().emit(record)


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number, got {value}")
    return number


def _language(value: str) -> str:
    if value.strip().lower() != "original" and len(value.strip()) != 3:
        raise argparse.ArgumentTypeError(f"must be a 3-letter ISO 639-2 code or 'original', got '{value}'")
    return value


def _timestamp(value: str) -> float:
    """`[[HH:]MM:]SS[.fff]` to seconds, as ffmpeg writes positions."""
    match = TIMESTAMP_PATTERN.fullmatch(value.strip())
    if match:
        hours, minutes, seconds = match.groups()
        # A unit below another one has to stay under 60, `1:75` is more likely a typo than 2:15
        if not (minutes is not None and float(seconds) >= 60 or hours is not None and int(minutes) >= 60):
            return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    raise argparse.ArgumentTypeError(f"must be a position as [[HH:]MM:]SS[.fff], got '{value}'")


def _undefined_language(value: str) -> str:
    # Unlike `_language`, no `original`: there is no original track to take a language from
    if len(value.strip()) != 3 or not value.strip().isalpha():
        raise argparse.ArgumentTypeError(f"must be a 3-letter ISO 639-2 code, got '{value}'")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    defaults = EncodingSettings()
    parser = argparse.ArgumentParser(prog="baycone-cli", description="Encode a single file the way the service does")
    parser.add_argument("input", type=Path, help="input file")
    parser.add_argument(
        "--output", type=Path, help=f"output file (default: <input dir>/{DIR_OUTPUT.as_posix()}/<name>.mp4)"
    )
    parser.add_argument(
        "--work-dir", type=Path, help=f"directory for temporary files (default: <input dir>/{DIR_WORK.as_posix()})"
    )
    parser.add_argument(
        "-l",
        "--language",
        dest="languages",
        action="append",
        type=_language,
        metavar="LANG",
        help=f"audio language to keep, repeatable, in order (default: {' '.join(defaults.target_audio_languages)})",
    )
    parser.add_argument(
        "-v",
        "--video-bitrate",
        type=_positive,
        default=defaults.target_video_bitrate,
        help="target video bitrate in kbps (default: %(default)s)",
    )
    parser.add_argument(
        "-a",
        "--audio-bitrate",
        type=_positive,
        default=defaults.target_audio_channel_bitrate,
        help="target audio bitrate per channel in kbps (default: %(default)s)",
    )
    parser.add_argument(
        "--codec",
        choices=list(ffmpeg.VIDEO_CODECS),
        default=defaults.target_video_codec,
        help="target video codec (default: %(default)s)",
    )
    parser.add_argument(
        "--subtitles",
        action="append",
        type=_language,
        default=[],
        metavar="LANG",
        help="subtitle language to extract, repeatable",
    )
    parser.add_argument(
        "--undefined-language",
        type=_undefined_language,
        metavar="LANG",
        help="language assumed for audio tracks without one (default: keep 'und')",
    )
    parser.add_argument(
        "--undefined-subtitle-language",
        type=_undefined_language,
        metavar="LANG",
        help="language assumed for subtitles without one (default: keep 'und')",
    )
    parser.add_argument(
        "--start",
        type=_timestamp,
        metavar="TIME",
        help="cut from this position, [[HH:]MM:]SS[.fff]; a copied video starts at the keyframe before it",
    )
    parser.add_argument("--end", type=_timestamp, metavar="TIME", help="cut at this position, [[HH:]MM:]SS[.fff]")
    # Remux copies every stream, a forced encode would be silently ignored
    copy_or_encode = parser.add_mutually_exclusive_group()
    copy_or_encode.add_argument(
        "--remux-only", action="store_true", help="never encode, only remux into the target container"
    )
    copy_or_encode.add_argument(
        "--force-video-encode",
        action="store_true",
        help="encode the video even when it is within every limit, so --start cuts it exactly",
    )
    parser.add_argument("--info", action="store_true", help="print the analysis as JSON and exit")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary (default: %(default)s)")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.start is not None and args.end is not None and args.start >= args.end:
        parser.error("--start must be before --end")
    return args


def build_settings(args: argparse.Namespace) -> EncodingSettings:
    settings = EncodingSettings(
        target_video_codec=args.codec,
        target_video_bitrate=args.video_bitrate,
        target_audio_channel_bitrate=args.audio_bitrate,
        target_subtitle_languages=args.subtitles,
        undefined_audio_language=args.undefined_language or "",
        undefined_subtitle_language=args.undefined_subtitle_language or "",
        force_video_encode=args.force_video_encode,
    )
    if args.languages:
        settings.target_audio_languages = args.languages
    return settings


def _to_json(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    return vars(value)


def _remove_work_dir(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)
    # Gone only when nothing else lives in it, `rmdir` never removes content
    with suppress(OSError):
        work_dir.parent.rmdir()


def _interrupt(console: Console, signum: int, frame: FrameType | None) -> None:
    # FFmpeg got the same SIGINT from the terminal; unwinding kills it and removes the work files
    console.end_line()
    print("Interrupted, cleaning up (press Ctrl+C again to force stop)", file=sys.stderr)
    signal.signal(signal.SIGINT, _force_stop)
    raise KeyboardInterrupt


def _force_stop(signum: int, frame: FrameType | None) -> NoReturn:
    # Skips the cleanup: the work dir is left behind, the output path never holds a partial file
    print("Forced stop", file=sys.stderr)
    os._exit(EXIT_INTERRUPTED)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_file = args.input.absolute()
    if not input_file.is_file():
        print(f"Invalid file path: '{input_file}'", file=sys.stderr)
        return EXIT_USAGE

    console = Console()
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[ConsoleHandler(console)], force=True)

    previous_handler = signal.signal(signal.SIGINT, partial(_interrupt, console))
    try:
        return _run(args, input_file, console)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _run(args: argparse.Namespace, input_file: Path, console: Console) -> int:
    settings = build_settings(args)
    tools = FFmpegTools(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)

    if args.info:
        info = Analyzer(settings, tools).analyze(input_file)
        print(json.dumps(info, default=_to_json, indent=2))
        return 0 if info.is_valid else EXIT_FAILED

    default_output = input_file.parent / DIR_OUTPUT / input_file.with_suffix(settings.target_extension).name
    output_file = (args.output or default_output).absolute()
    work_dir = (args.work_dir or input_file.parent / DIR_WORK).absolute()

    job = Job(input_file=input_file, output_file=output_file, work_dir=work_dir, start=args.start, end=args.end)
    try:
        Encoder(settings, tools, on_progress=console.progress).process(job, remux_only=args.remux_only)
    except BayconeError as e:
        console.end_line()
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as e:
        console.end_line()
        print(f"Unexpected error: {e}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        if args.work_dir is None:
            _remove_work_dir(work_dir)
    console.end_line()
    return 0
