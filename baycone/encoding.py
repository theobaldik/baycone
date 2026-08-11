from copy import deepcopy, copy
import logging
import subprocess
import json
import shutil
from pathlib import Path
from dataclasses import dataclass

from baycone import ffmpeg
from baycone.config import config, LocationConfig, SpecialDirs

log = logging.getLogger(__name__)

# Single-track audio only for now; 'target_audio_languages' is not honoured yet.
DEFAULT_AUDIO_LANGUAGE = "eng"


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
        match self.channels:
            case 1:
                return "Mono"
            case 2:
                return "Stereo"
            case 3:
                return "Stereo 2.1"
            case 6:
                return "Surround 5.1"
            case 8:
                return "Surround 7.1"
            case 9:
                return "Surround 7.2"
            case _:
                return ""


class FileInfo:
    def __init__(self, file_path: Path, **kwargs):
        self._file_path: Path = None
        self._special_dirs: SpecialDirs = None
        self._location_config: LocationConfig = None

        self.is_valid: bool = kwargs.get("is_valid", False)
        self.shall_encode: bool = kwargs.get("shall_encode", False)

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
        return copy(self._file_path)

    @file_path.setter
    def file_path(self, value: Path) -> None:
        if value is None:
            raise ValueError("'file_path' property cannot be set to None")

        if not isinstance(value, Path):
            raise TypeError("'file_path' property must be set to 'Path' object")

        self._file_path = value

        # Get location config
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
    def _get_file_info(file_path: Path) -> FileInfo:
        probe_result = Analyzer._get_ffprobe_result(file_path)

        if not probe_result:
            return FileInfo(file_path=file_path, is_valid=False)

        file_info = FileInfo(file_path=file_path, is_valid=True)

        # Extract container format
        format_info = probe_result.get("format", {})
        file_info.containers = format_info.get("format_name", "").split(",")
        file_info.duration = int(float(format_info.get("duration", 0)))

        # Process streams
        streams = probe_result.get("streams", [])

        # Extract video stream info
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        if video_streams:
            video = video_streams[0]
            file_info.video_stream.codec = video.get("codec_name", "")
            file_info.video_stream.bitrate = int(video.get("bit_rate", 0)) // 1000

        # Extract audio stream info
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        for stream in audio_streams:
            audio_stream = AudioStream()

            audio_stream.codec = stream.get("codec_name", "")
            audio_stream.channels = int(stream.get("channels", 1))
            if audio_stream.channels > 0:
                audio_stream.channel_bitrate = (int(stream.get("bit_rate", 0)) // 1000) // audio_stream.channels

            tags = stream.get("tags", {})
            audio_stream.lang = tags.get("language") or tags.get("LANGUAGE") or tags.get("lang", "")

            file_info.audio_streams.append(audio_stream)

        return file_info

    @staticmethod
    def analyze(file_path: Path) -> FileInfo:
        file_info = Analyzer._get_file_info(file_path)

        if not any(c in file_info.location_config.allowed_containers for c in file_info.containers):
            file_info.shall_encode = True

        if file_info.video_stream.codec not in file_info.location_config.allowed_video_codecs:
            file_info.video_stream.shall_encode = True
        if file_info.video_stream.bitrate > file_info.location_config.limit_video_bitrate:
            file_info.video_stream.shall_encode = True

        for stream in file_info.audio_streams:
            if stream.codec not in file_info.location_config.allowed_audio_codecs:
                stream.shall_encode = True
            if stream.channels > 0 and stream.channel_bitrate > file_info.location_config.limit_audio_channel_bitrate:
                stream.shall_encode = True

        return file_info


class Encoder:

    def __init__(self):
        self._processing_file: FileInfo | None = None

    def process(self, file_path: Path) -> None:
        self._processing_file = Analyzer.analyze(file_path)

        try:
            shall_encode = (
                self._processing_file.shall_encode
                or self._processing_file.video_stream.shall_encode
                or any(s.shall_encode for s in self._processing_file.audio_streams)
            )

            # The original must stay untouched unless the whole chain succeeded.
            if shall_encode and not self._encode():
                log.error(f"Processing aborted: Encoding failed for '{file_path}'")
                return

            if not self._optimize(shall_encode):
                log.error(f"Processing aborted: Optimizing failed for '{file_path}'")
                return

            self._archive_original()
        finally:
            self._cleanup_workdirs()
            self._processing_file = None

    def _log_progress(self, process: subprocess.Popen, template: str = "{percent} %"):
        duration = self._processing_file.duration
        percent = 0

        log.info(template.format(percent=percent))

        # The pipe must be drained even when the progress cannot be computed, otherwise FFmpeg blocks on a full pipe.
        for line in process.stdout:
            key, _, value = line.strip().partition("=")

            if duration and key == "out_time_us" and value.isdigit():
                out_percent = int(value) / 10_000 / duration
                if out_percent >= percent + 10:
                    percent = int(out_percent // 10) * 10
                    log.info(template.format(percent=percent))

    def _encode(self) -> bool:
        if not self._processing_file.is_valid:
            log.error(f"Encoding skipped: Invalid file for encoding: '{self._processing_file.file_path}'")
            return False

        if not self._processing_file.audio_streams:
            log.error(f"Encoding skipped: No audio streams found in '{self._processing_file.file_path}'")
            return False

        self._processing_file.passlog_path.unlink(missing_ok=True)
        self._processing_file.encoding_path.unlink(missing_ok=True)

        # TODO: Implement multitrack audio
        audio_bitrate = (
            self._processing_file.location_config.target_audio_channel_bitrate
            * self._processing_file.audio_streams[0].channels
        )

        try:
            # 1st pass
            self._processing_file.passlog_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = ffmpeg.get_cmd_encode_movie_pass_1(
                self._processing_file.file_path,
                self._processing_file.passlog_path,
                min(
                    self._processing_file.video_stream.bitrate,
                    self._processing_file.location_config.target_video_bitrate,
                ),
            )
            log.info("Encoding file...")
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self._log_progress(process, "Encoding 1st pass: {percent} %")
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)
            log.info("1st pass encoding finished")

            # 2nd pass
            self._processing_file.encoding_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = ffmpeg.get_cmd_encode_movie_pass_2(
                self._processing_file.file_path,
                self._processing_file.encoding_path,
                self._processing_file.passlog_path,
                min(
                    self._processing_file.video_stream.bitrate,
                    self._processing_file.location_config.target_video_bitrate,
                ),
                min(self._processing_file.audio_streams[0].overall_bitrate, audio_bitrate),
            )
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self._log_progress(process, "Encoding 2nd pass: {percent} %")
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)
            log.info(f"Encoded file saved to '{self._processing_file.encoding_path}'")

            return True

        except subprocess.CalledProcessError as e:
            log.error(f"FFmpeg encoding failed for '{self._processing_file.file_path}': {e.stderr}")

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
            log.info(f"Optimizing file...")
            subprocess.run(cmd, check=True, text=True, capture_output=True)
            log.info(f"Optimized file saved to '{self._processing_file.processed_path}'")
            return True
        except subprocess.CalledProcessError as e:
            log.error(f"FFmpeg optimization failed for {self._processing_file.file_path}: {e.stderr}")
        except subprocess.TimeoutExpired:
            log.error(f"FFmpeg optimization timeout for {self._processing_file.file_path}")
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
        """Removes `directory` and its parents up to (excluding) `root_dir` as long as they are empty."""
        while directory != root_dir and directory.is_relative_to(root_dir):
            try:
                # Only ever succeeds on an empty directory - never removes unprocessed files.
                directory.rmdir()
            except OSError:
                return
            log.info(f"Removed empty directory '{directory}'")
            directory = directory.parent

    def _archive_original(self) -> None:
        """Moves the original file to the originals directory and prunes the directories it left empty."""
        source_path = self._processing_file.file_path
        target_path = self._processing_file.original_path

        target_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.rename(target_path)
        log.info(f"File '{source_path}' moved to '{target_path}'")

        self._remove_empty_dirs(source_path.parent, self._processing_file.special_dirs.root_dir)

    def _cleanup_workdirs(self) -> None:
        """Clears the temporary work directories."""
        self._clear_dir(self._processing_file.special_dirs.passlog_dir)
        self._clear_dir(self._processing_file.special_dirs.encoded_dir)
