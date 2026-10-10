from copy import deepcopy
import logging
import subprocess
import json
import shutil
from pathlib import Path
from dataclasses import dataclass

from baycone import ffmpeg
from baycone.config import config, LocationConfig, SpecialDirs

log = logging.getLogger(__name__)

DEFAULT_AUDIO_LANGUAGE = "eng"

TRACK_NAMES: dict[int, str] = {
    1: "Mono",
    2: "Stereo",
    3: "Stereo 2.1",
    6: "Surround 5.1",
    8: "Surround 7.1",
    9: "Surround 7.2",
}


def _parse_number(value: object) -> float:
    """ffprobe reports unknown values as missing or 'N/A'."""
    try:
        return float(value)
    except TypeError, ValueError:
        return 0.0


@dataclass
class VideoStream:
    shall_encode: bool = False
    codec: str = ""
    bitrate: int = 0


@dataclass
class AudioStream:
    shall_encode: bool = False
    codec: str = ""
    channel_bitrate: int = 0
    channels: int = 0
    lang: str = ""
    title: str = ""

    @property
    def overall_bitrate(self) -> int:
        return self.channel_bitrate * self.channels

    def generate_track_name(self) -> str:
        return TRACK_NAMES.get(self.channels, "")


class FileInfo:
    def __init__(self, file_path: Path, **kwargs):
        self._file_path: Path = None
        self._special_dirs: SpecialDirs = None
        self._location_config: LocationConfig = None

        self.is_valid: bool = kwargs.get("is_valid", False)
        self.shall_remux: bool = kwargs.get("shall_remux", False)

        self.duration: int = kwargs.get("duration", 0)
        self.containers: list[str] = kwargs.get("containers", [])

        self.video_stream: VideoStream = kwargs.get("video_stream", VideoStream())
        self.audio_streams: list[AudioStream] = kwargs.get("audio_streams", [])

        self.file_path = file_path

    @property
    def location_config(self) -> LocationConfig:
        return deepcopy(self._location_config)

    @property
    def special_dirs(self) -> SpecialDirs:
        return self._special_dirs

    @property
    def file_path(self) -> Path:
        return self._file_path

    @file_path.setter
    def file_path(self, value: Path) -> None:
        if value is None:
            raise ValueError("'file_path' property cannot be set to None")

        if not isinstance(value, Path):
            raise TypeError("'file_path' property must be set to 'Path' object")

        self._file_path = value

        for lc in config.locations:
            if value.is_relative_to(lc.abspath):
                self._location_config = lc
                self._special_dirs = SpecialDirs(lc.abspath)
                break
        else:
            raise ValueError(f"No 'LocationConfig' found for '{value}'")

    @property
    def relative_path(self) -> Path:
        return self.file_path.relative_to(self._special_dirs.root_dir)

    @property
    def passlog_path(self) -> Path:
        return self._special_dirs.passlog_dir / self.relative_path

    @property
    def encoding_path(self) -> Path:
        return self._special_dirs.encoded_dir / self.relative_path.with_suffix(self._location_config.target_extension)

    @property
    def original_path(self) -> Path:
        return self._special_dirs.originals_dir / self.relative_path

    @property
    def processed_path(self) -> Path:
        return self._special_dirs.processed_dir / self.relative_path.with_suffix(self._location_config.target_extension)


class Analyzer:

    @staticmethod
    def _get_ffprobe_result(file_path: Path) -> dict | None:
        if not file_path.is_file():
            return None
        try:
            cmd = ffmpeg.get_cmd_ffprobe(file_path)
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            log.error(f"FFprobe command failed for {file_path}: {e.stderr}")
            return None
        except subprocess.TimeoutExpired:
            log.error(f"FFprobe timeout for {file_path}")
            return None
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.error(f"Error parsing ffprobe output for {file_path}: {e}")
            return None
        except Exception as e:
            log.error(f"Unexpected error during ffprobe for {file_path}: {e}")
            return None

    @staticmethod
    def _get_bitrate(stream: dict) -> int:
        """In kbps, 0 when unknown. Matroska only reports it in the 'BPS' tag."""
        bitrate = stream.get("bit_rate") or stream.get("tags", {}).get("BPS")
        return int(_parse_number(bitrate)) // 1000

    @staticmethod
    def _get_file_info(file_path: Path) -> FileInfo:
        probe_result = Analyzer._get_ffprobe_result(file_path)

        if not probe_result:
            return FileInfo(file_path=file_path, is_valid=False)

        file_info = FileInfo(file_path=file_path, is_valid=True)

        format_info = probe_result.get("format", {})
        file_info.containers = format_info.get("format_name", "").split(",")
        file_info.duration = int(_parse_number(format_info.get("duration")))

        streams = probe_result.get("streams", [])

        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        if video_streams:
            video = video_streams[0]
            file_info.video_stream.codec = video.get("codec_name", "")
            file_info.video_stream.bitrate = Analyzer._get_bitrate(video)

        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        for stream in audio_streams:
            audio_stream = AudioStream()

            audio_stream.codec = stream.get("codec_name", "")
            audio_stream.channels = int(_parse_number(stream.get("channels", 1)))
            if audio_stream.channels > 0:
                audio_stream.channel_bitrate = Analyzer._get_bitrate(stream) // audio_stream.channels

            tags = stream.get("tags", {})
            audio_stream.lang = tags.get("language") or tags.get("LANGUAGE") or tags.get("lang", "")

            file_info.audio_streams.append(audio_stream)

        return file_info

    @staticmethod
    def analyze(file_path: Path) -> FileInfo:
        file_info = Analyzer._get_file_info(file_path)
        location_config = file_info.location_config

        if not any(c in location_config.allowed_containers for c in file_info.containers):
            file_info.shall_remux = True

        if file_info.video_stream.codec not in location_config.allowed_video_codecs:
            file_info.video_stream.shall_encode = True
        if file_info.video_stream.bitrate > location_config.limit_video_bitrate:
            file_info.video_stream.shall_encode = True

        for stream in file_info.audio_streams:
            if stream.codec not in location_config.allowed_audio_codecs:
                stream.shall_encode = True
            if stream.channels > 0 and stream.channel_bitrate > location_config.limit_audio_channel_bitrate:
                stream.shall_encode = True

        return file_info


class Encoder:

    def __init__(self):
        self._processing_file: FileInfo | None = None

    def process(self, file_path: Path) -> bool:
        self._processing_file = Analyzer.analyze(file_path)

        try:
            # A container mismatch alone is fixed by the remux in `_optimize`
            shall_encode = self._processing_file.video_stream.shall_encode or any(
                s.shall_encode for s in self._processing_file.audio_streams
            )
            if not shall_encode and self._processing_file.shall_remux:
                log.info(f"Only the container differs, remuxing '{file_path}'")

            # The original stays in place unless the whole chain succeeds
            if shall_encode and not self._encode():
                log.error(f"Processing aborted: Encoding failed for '{file_path}'")
                return False

            if not self._optimize(shall_encode):
                log.error(f"Processing aborted: Optimizing failed for '{file_path}'")
                return False

            self._archive_original()
            return True
        finally:
            self._cleanup_workdirs()
            self._processing_file = None

    def _log_progress(self, process: subprocess.Popen, template: str = "{percent} %") -> None:
        duration = self._processing_file.duration
        percent = 0

        log.info(template.format(percent=percent))

        # Drain the pipe even without a duration, otherwise FFmpeg blocks on a full pipe
        for line in process.stdout:
            key, _, value = line.strip().partition("=")

            if duration and key == "out_time_us" and value.isdigit():
                out_percent = int(value) / 10_000 / duration
                if out_percent >= percent + 10:
                    percent = int(out_percent // 10) * 10
                    log.info(template.format(percent=percent))

    def _run_ffmpeg(self, cmd: list[str], progress_template: str) -> None:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            self._log_progress(process, progress_template)
            process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)

    @staticmethod
    def _cap_bitrate(source: int, target: int) -> int:
        """Never upscales, falls back to `target` when the source bitrate is unknown."""
        return min(source, target) if source > 0 else target

    def _encode(self) -> bool:
        file_info = self._processing_file

        if not file_info.is_valid:
            log.error(f"Encoding skipped: Invalid file for encoding: '{file_info.file_path}'")
            return False

        if not file_info.audio_streams:
            log.error(f"Encoding skipped: No audio streams found in '{file_info.file_path}'")
            return False

        location_config = file_info.location_config
        # TODO: Implement multitrack audio
        audio_stream = file_info.audio_streams[0]
        audio_bitrate = self._cap_bitrate(
            audio_stream.overall_bitrate, location_config.target_audio_channel_bitrate * audio_stream.channels
        )
        video_bitrate = self._cap_bitrate(file_info.video_stream.bitrate, location_config.target_video_bitrate)

        file_info.encoding_path.parent.mkdir(parents=True, exist_ok=True)
        file_info.encoding_path.unlink(missing_ok=True)

        try:
            if file_info.video_stream.shall_encode:
                file_info.passlog_path.parent.mkdir(parents=True, exist_ok=True)
                log.info("Encoding file...")
                cmd = ffmpeg.get_cmd_encode_movie_pass_1(file_info.file_path, file_info.passlog_path, video_bitrate)
                self._run_ffmpeg(cmd, "Encoding 1st pass: {percent} %")
                log.info("1st pass encoding finished")

                cmd = ffmpeg.get_cmd_encode_movie_pass_2(
                    file_info.file_path, file_info.encoding_path, file_info.passlog_path, video_bitrate, audio_bitrate
                )
                self._run_ffmpeg(cmd, "Encoding 2nd pass: {percent} %")
            else:
                log.info("Encoding audio, copying video...")
                cmd = ffmpeg.get_cmd_encode_audio(file_info.file_path, file_info.encoding_path, audio_bitrate)
                self._run_ffmpeg(cmd, "Encoding audio: {percent} %")

            log.info(f"Encoded file saved to '{file_info.encoding_path}'")
            return True

        except subprocess.CalledProcessError as e:
            log.error(f"FFmpeg encoding failed for '{file_info.file_path}' with exit code {e.returncode}")

        return False

    def _optimize(self, encoded: bool) -> bool:
        if not self._processing_file.audio_streams:
            log.error(f"Optimizing skipped: No audio streams found in '{self._processing_file.file_path}'")
            return False

        # TODO: Implement multitrack audio and honour 'location_config.target_audio_languages'
        language = DEFAULT_AUDIO_LANGUAGE
        track_name = self._processing_file.audio_streams[0].generate_track_name()

        self._processing_file.processed_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ffmpeg.get_cmd_optimize(
            self._processing_file.encoding_path if encoded else self._processing_file.file_path,
            self._processing_file.processed_path,
            language,
            track_name,
        )

        try:
            log.info("Optimizing file...")
            subprocess.run(cmd, check=True, text=True, capture_output=True)
            log.info(f"Optimized file saved to '{self._processing_file.processed_path}'")
            return True
        except subprocess.CalledProcessError as e:
            log.error(f"FFmpeg optimization failed for {self._processing_file.file_path}: {e.stderr}")
        except Exception as e:
            log.error(f"Unexpected error during FFmpeg optimization for {self._processing_file.file_path}: {e}")

        return False

    # TODO: Move to another module
    @staticmethod
    def _clear_dir(directory: Path) -> None:
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _remove_empty_dirs(directory: Path, root_dir: Path) -> None:
        """Stops at `root_dir` (exclusive) or the first non-empty directory."""
        while directory != root_dir and directory.is_relative_to(root_dir):
            try:
                # Fails on non-empty directories, so unprocessed files are never removed
                directory.rmdir()
            except OSError:
                return
            log.info(f"Removed empty directory '{directory}'")
            directory = directory.parent

    def _archive_original(self) -> None:
        source_path = self._processing_file.file_path
        target_path = self._processing_file.original_path

        target_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.rename(target_path)
        log.info(f"File '{source_path}' moved to '{target_path}'")

        self._remove_empty_dirs(source_path.parent, self._processing_file.special_dirs.root_dir)

    def _cleanup_workdirs(self) -> None:
        self._clear_dir(self._processing_file.special_dirs.passlog_dir)
        self._clear_dir(self._processing_file.special_dirs.encoded_dir)
