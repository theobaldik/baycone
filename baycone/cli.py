import json
import shutil
import sys
import argparse
import subprocess
from argparse import Namespace
from pathlib import Path

from baycone import ffmpeg
from baycone.audio import AudioStream, AudioTrack
from baycone.config import Constants

EXT_PROCESSED = ".mp4"


# region Directories and files


def get_dir_temp(input_file: Path) -> Path:
    return input_file.parent / Constants.DIR_TEMP


def get_dir_processed(input_file: Path) -> Path:
    return input_file.parent / Constants.DIR_PROCESSED


def get_file_passlog(input_file: Path) -> Path:
    return get_dir_temp(input_file) / input_file.name


def get_file_transcoded(input_file: Path) -> Path:
    return get_dir_temp(input_file) / input_file.with_suffix(EXT_PROCESSED).name


def get_file_processed(input_file: Path) -> Path:
    return get_dir_processed(input_file) / input_file.with_suffix(EXT_PROCESSED).name


def prepare_dirs(input_file: Path, include_temp: bool) -> None:
    if include_temp:
        temp_dir = get_dir_temp(input_file)
        if temp_dir.is_dir():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir()

    processed_dir = get_dir_processed(input_file)

    if not processed_dir.is_dir():
        processed_dir.mkdir()


def clear_dirs(input_file: Path) -> None:
    temp_dir = get_dir_temp(input_file)
    if temp_dir.is_dir():
        shutil.rmtree(temp_dir)


# endregion Directories and files

# region Transcoding


def get_file_info(file_path: Path) -> dict:
    cmd = ffmpeg.get_cmd_ffprobe(file_path)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def print_progress(process: subprocess.Popen, duration: float, template: str) -> None:
    percent = 0.0

    print(template.format(percent=percent), end="", flush=True)

    # Drain the pipe even without a duration, otherwise FFmpeg blocks on a full pipe
    for line in process.stdout:
        key, _, value = line.strip().partition("=")

        if duration > 0 and key == "out_time_us" and value.isdigit():
            out_percent = float(value) / 10_000.0 / duration
            if out_percent > percent:
                percent = out_percent
                print(f"\r{template.format(percent=percent)}", end="", flush=True)


def run_process(command: list[str], duration: float, progress_template: str) -> None:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    print_progress(process, duration, progress_template)
    process.wait()
    print()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)


def transcode_1_pass(input_file: Path, video_bitrate: int, duration: float) -> None:
    passlog_file = get_file_passlog(input_file)
    cmd = ffmpeg.get_cmd_encode_movie_pass_1(input_file, passlog_file, video_bitrate, [])
    run_process(cmd, duration, "Transcoding 1st pass: {percent:.2f} %")


def transcode_2_pass(input_file: Path, video_bitrate: int, audio_track: AudioTrack, duration: float) -> None:
    passlog_file = get_file_passlog(input_file)
    output_file = get_file_transcoded(input_file)
    cmd = ffmpeg.get_cmd_encode_movie_pass_2(input_file, output_file, passlog_file, video_bitrate, [], [audio_track])
    run_process(cmd, duration, "Transcoding 2nd pass: {percent:.2f} %")


def optimize(input_file: Path, output_file: Path, audio_track: AudioTrack, encoded: bool, duration: float) -> None:
    cmd = ffmpeg.get_cmd_optimize(input_file, output_file, [audio_track], encoded)
    run_process(cmd, duration, "Optimizing: {percent:.2f} %")


# endregion Transcoding


# region Application


def cli() -> Namespace:
    parser = argparse.ArgumentParser(prog="Baycone CLI", description="CLI executor of Baycone transcoder")
    parser.add_argument("input", help="input file path")
    parser.add_argument("-l", "--language", type=str, default="eng", help="audio track language")
    parser.add_argument("-v", "--video-bitrate", type=int, default=2250, help="target video bitrate in kbps")
    parser.add_argument("-a", "--audio-bitrate", type=int, default=64, help="target audio bitrate per channel in kbps")
    parser.add_argument("-o", "--optimize", action="store_true", help="skip transcoding and only optimize the file")
    parser.add_argument("-i", "--info", action="store_true", help="skip transcoding and display file information")
    return parser.parse_args()


def arg_check(args: Namespace) -> bool:
    input_path = Path(args.input).absolute()

    if not input_path.is_file():
        print(f"Invalid file path: '{input_path}'", file=sys.stderr)
        return False

    if args.info:
        return True

    if len(args.language) != 3:
        print("Language must be 3 characters long", file=sys.stderr)
        return False

    if args.audio_bitrate <= 0:
        print("Audio bitrate must be a positive number", file=sys.stderr)
        return False

    if args.video_bitrate <= 0:
        print("Video bitrate must be a positive number", file=sys.stderr)
        return False

    return True


def main() -> int:
    try:
        args = cli()

        if not arg_check(args):
            return 1

        input_path = Path(args.input).absolute()
        processed_path = get_file_processed(input_path)
        file_info = get_file_info(input_path)

        if args.info:
            print(file_info)
            return 0

        duration = float(file_info.get("format", {}).get("duration", 0))

        audio_streams = [s for s in file_info.get("streams", []) if s.get("codec_type") == "audio"]
        audio_channels = int(audio_streams[0].get("channels", 1)) if audio_streams else 1

        # The CLI keeps only the first audio stream
        audio_track = AudioTrack(
            AudioStream(channels=audio_channels, lang=args.language),
            bitrate=args.audio_bitrate * audio_channels,
            default=True,
        )

        prepare_dirs(input_path, not args.optimize)

        if args.optimize:
            optimize(input_path, processed_path, audio_track, False, duration)
            return 0

        output_path = get_file_transcoded(input_path)

        transcode_1_pass(input_path, args.video_bitrate, duration)
        transcode_2_pass(input_path, args.video_bitrate, audio_track, duration)
        optimize(output_path, processed_path, audio_track, True, duration)

        clear_dirs(input_path)

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


# endregion Application

if __name__ == "__main__":
    sys.exit(main())
