import argparse
import json
import logging
import shutil
import sys
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from pathlib import Path

from baycone import ffmpeg
from baycone.encoding import Analyzer, Encoder, Job
from baycone.errors import BayconeError
from baycone.settings import EncodingSettings, FFmpegTools

log = logging.getLogger(__name__)

# A directory of the CLI's own: `.baycone` itself may hold a running service's state and work files
DIR_WORK = Path(".baycone") / "cli"
DIR_OUTPUT = Path("_baycone_processed")


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
    parser.add_argument("--remux-only", action="store_true", help="never encode, only remux into the target container")
    parser.add_argument("--info", action="store_true", help="print the analysis as JSON and exit")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary (default: %(default)s)")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary (default: %(default)s)")
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> EncodingSettings:
    settings = EncodingSettings(
        target_video_codec=args.codec,
        target_video_bitrate=args.video_bitrate,
        target_audio_channel_bitrate=args.audio_bitrate,
        target_subtitle_languages=args.subtitles,
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_file = args.input.absolute()
    if not input_file.is_file():
        print(f"Invalid file path: '{input_file}'", file=sys.stderr)
        return 1

    console = Console()
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[ConsoleHandler(console)], force=True)

    settings = build_settings(args)
    tools = FFmpegTools(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)

    if args.info:
        info = Analyzer(settings, tools).analyze(input_file)
        print(json.dumps(info, default=_to_json, indent=2))
        return 0 if info.is_valid else 2

    default_output = input_file.parent / DIR_OUTPUT / input_file.with_suffix(settings.target_extension).name
    output_file = (args.output or default_output).absolute()
    work_dir = (args.work_dir or input_file.parent / DIR_WORK).absolute()

    job = Job(input_file=input_file, output_file=output_file, work_dir=work_dir)
    try:
        Encoder(settings, tools, on_progress=console.progress).process(job, remux_only=args.remux_only)
    except BayconeError as e:
        console.end_line()
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        console.end_line()
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 2
    finally:
        if args.work_dir is None:
            _remove_work_dir(work_dir)
    console.end_line()
    return 0
