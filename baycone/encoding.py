from copy import deepcopy
import logging
import subprocess
import json
import re
import shutil
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from dataclasses import dataclass

from baycone import ffmpeg
from baycone.audio import (
    SPECIAL_DISPOSITIONS,
    AudioStream,
    AudioTrack,
    cap_bitrate,
    normalize_language,
    select_audio_tracks,
)
from baycone.config import config, LocationConfig, SpecialDirs
from baycone.subtitles import SRT_CODECS, SubtitleStream, ensure_utf8, language_suffix, select_subtitle_streams

log = logging.getLogger(__name__)

HDR_TRANSFERS: tuple[str, ...] = ("smpte2084", "arib-std-b67")

CROP_PATTERN = re.compile(r"crop=\d+:(\d+):\d+:(\d+)")


def _format_duration(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def _format_bitrate(kbps: int) -> str:
    return f"{kbps} kbps" if kbps > 0 else "unknown bitrate"


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
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    color_transfer: str = ""
    crop_height: int = 0  # 0 keeps the full height
    crop_top: int = 0

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS


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
        self.audio_tracks: list[AudioTrack] = kwargs.get("audio_tracks", [])
        self.subtitle_streams: list[SubtitleStream] = kwargs.get("subtitle_streams", [])
        self.subtitle_tracks: list[SubtitleStream] = kwargs.get("subtitle_tracks", [])

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

    def subtitle_path(self, lang: str) -> Path:
        return self.processed_path.with_suffix(f".{language_suffix(lang)}.srt")


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

        # Cover art is a video stream too; it is never the movie and is always dropped (`0:V` in the commands)
        video_streams = [
            s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ]
        if video_streams:
            video = video_streams[0]
            file_info.video_stream.codec = video.get("codec_name", "")
            file_info.video_stream.bitrate = Analyzer._get_bitrate(video)
            file_info.video_stream.width = int(_parse_number(video.get("width")))
            file_info.video_stream.height = int(_parse_number(video.get("height")))
            file_info.video_stream.pix_fmt = video.get("pix_fmt", "")
            file_info.video_stream.color_transfer = video.get("color_transfer", "")

        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        for index, stream in enumerate(audio_streams):
            audio_stream = AudioStream(index=index)

            audio_stream.codec = stream.get("codec_name", "")
            audio_stream.channels = int(_parse_number(stream.get("channels", 1)))
            if audio_stream.channels > 0:
                audio_stream.channel_bitrate = Analyzer._get_bitrate(stream) // audio_stream.channels

            tags = stream.get("tags", {})
            audio_stream.lang = normalize_language(tags.get("language") or tags.get("LANGUAGE") or tags.get("lang", ""))

            disposition = stream.get("disposition", {})
            audio_stream.original = bool(disposition.get("original"))
            audio_stream.special = any(disposition.get(name) for name in SPECIAL_DISPOSITIONS)

            file_info.audio_streams.append(audio_stream)

        subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
        for index, stream in enumerate(subtitle_streams):
            tags = stream.get("tags", {})
            disposition = stream.get("disposition", {})
            file_info.subtitle_streams.append(
                SubtitleStream(
                    index=index,
                    codec=stream.get("codec_name", ""),
                    lang=normalize_language(tags.get("language") or tags.get("LANGUAGE") or tags.get("lang", "")),
                    special=any(disposition.get(name) for name in SPECIAL_DISPOSITIONS),
                )
            )

        return file_info

    @staticmethod
    def detect_crop(file_path: Path, video: VideoStream) -> None:
        """Top and bottom bars only, the full width is always kept."""
        log.info("Detecting black bars...")
        try:
            result = subprocess.run(ffmpeg.get_cmd_cropdetect(file_path), capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            log.warning(f"Black bar detection failed, keeping the full frame: {e.stderr.strip()[-500:]}")
            return

        matches = CROP_PATTERN.findall(result.stderr)
        height, top = map(int, matches[-1]) if matches else (0, 0)
        if 0 < height < video.height:
            video.crop_height, video.crop_top = height, top
            log.info(f"Black bars detected, cropping to {video.width}x{height}")
        else:
            log.info("No black bars detected")

    @staticmethod
    def analyze(file_path: Path) -> FileInfo:
        file_info = Analyzer._get_file_info(file_path)
        location_config = file_info.location_config

        if not any(c in location_config.allowed_containers for c in file_info.containers):
            file_info.shall_remux = True

        # An unknown bitrate cannot be checked against the limit, the encode then targets the configured one
        video = file_info.video_stream
        if (
            video.codec not in location_config.allowed_video_codecs
            or not 0 < video.bitrate <= location_config.limit_video_bitrate
            or video.pix_fmt != ffmpeg.TARGET_PIX_FMT
            or video.width > location_config.limit_video_width
            or video.height > location_config.limit_video_height
        ):
            video.shall_encode = True

        for stream in file_info.audio_streams:
            if stream.codec not in location_config.allowed_audio_codecs:
                stream.shall_encode = True
            if not 0 < stream.channel_bitrate <= location_config.limit_audio_channel_bitrate:
                stream.shall_encode = True

        file_info.audio_tracks = select_audio_tracks(file_info.audio_streams, location_config)
        file_info.subtitle_tracks = select_subtitle_streams(file_info.subtitle_streams, location_config)

        return file_info


class Encoder:

    def __init__(self):
        self._processing_file: FileInfo | None = None

    def process(self, file_path: Path) -> bool:
        self._processing_file = Analyzer.analyze(file_path)

        try:
            if not self._processing_file.is_valid:
                log.error(f"Processing aborted: Invalid file '{file_path}'")
                return False

            log.info(
                f"Processing '{self._processing_file.relative_path}'"
                f" ({_format_duration(self._processing_file.duration)})"
            )
            # Decodes keyframes of the whole file, only worth it when the video is encoded anyway
            if self._processing_file.video_stream.shall_encode:
                Analyzer.detect_crop(file_path, self._processing_file.video_stream)
            self._log_plan()

            # A container mismatch or dropped audio tracks alone are fixed by the remux in `_optimize`
            shall_encode = self._processing_file.video_stream.shall_encode or any(
                t.stream.shall_encode for t in self._processing_file.audio_tracks
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

            self._extract_subtitles()

            self._archive_original()
            return True
        finally:
            self._cleanup_workdirs()
            self._processing_file = None

    def _target_video_bitrate(self) -> int:
        return cap_bitrate(
            self._processing_file.video_stream.bitrate, self._processing_file.location_config.target_video_bitrate
        )

    def _video_filters(self) -> dict[str, str]:
        """Description -> ffmpeg filter, in the order they apply."""
        video = self._processing_file.video_stream
        location_config = self._processing_file.location_config
        filters = {}

        height = video.height
        if video.crop_height:
            height = video.crop_height
            filters[f"crop to {video.width}x{height}"] = ffmpeg.get_filter_crop(height, video.crop_top)

        # Never upscales; scaling before tone mapping leaves fewer pixels for the expensive float conversion
        box_width, box_height = location_config.limit_video_width, location_config.limit_video_height
        if video.width > box_width or height > box_height:
            filters[f"scale to fit {box_width}x{box_height}"] = ffmpeg.get_filter_scale(box_width, box_height)

        if video.is_hdr:
            filters["tone map to SDR"] = ffmpeg.get_filter_tonemap(video.color_transfer)

        return filters

    def _log_plan(self) -> None:
        file_info = self._processing_file
        video = file_info.video_stream
        source = f"{video.codec} {video.width}x{video.height}{' HDR' if video.is_hdr else ''}"
        action = "copy"
        if video.shall_encode:
            action = f"encode to h264 {_format_bitrate(self._target_video_bitrate())}"
            if filters := self._video_filters():
                action += f" ({', '.join(filters)})"
        log.info(f"Video: {source} {_format_bitrate(video.bitrate)}, {action}")

        tracks = []
        for track in file_info.audio_tracks:
            stream = track.stream
            action = (
                f"{stream.codec} -> aac {_format_bitrate(track.bitrate)}" if track.bitrate else f"{stream.codec}, copy"
            )
            tracks.append(f"{stream.lang} {track.title or f'{stream.channels} ch'} ({action})")
        dropped = len(file_info.audio_streams) - len(file_info.audio_tracks)
        log.info(f"Audio: {', '.join(tracks) or 'none'}; {dropped} track(s) dropped")

        if file_info.location_config.target_subtitle_languages:
            languages = [s.lang for s in file_info.subtitle_tracks]
            log.info(f"Subtitles: {', '.join(languages) or 'none'} extracted")

    def _log_progress(self, process: subprocess.Popen, step: str) -> None:
        name = self._processing_file.relative_path
        duration = self._processing_file.duration
        started = time.monotonic()
        percent = 0

        log.info(f"'{name}' {step}: 0 %")

        # Drain the pipe even without a duration, otherwise FFmpeg blocks on a full pipe
        for line in process.stdout:
            key, _, value = line.strip().partition("=")

            if duration and key == "out_time_us" and value.isdigit():
                out_percent = int(value) / 10_000 / duration
                if out_percent >= percent + 10:
                    percent = int(out_percent // 10) * 10
                    eta = (time.monotonic() - started) * max(100 - out_percent, 0) / out_percent
                    log.info(f"'{name}' {step}: {percent} %, ETA {_format_duration(eta)}")

    def _run_ffmpeg(self, cmd: list[str], step: str) -> None:
        started = time.monotonic()
        # A file rather than a pipe, which would block FFmpeg once full while only stdout is drained
        with tempfile.TemporaryFile(mode="w+") as stderr:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True)
            try:
                self._log_progress(process, step)
                process.wait()
            except BaseException:
                process.kill()
                process.wait()
                raise
            if process.returncode != 0:
                stderr.seek(0)
                raise subprocess.CalledProcessError(process.returncode, cmd, stderr=stderr.read().strip())
        log.info(f"{step} finished in {_format_duration(time.monotonic() - started)}")

    def _encode(self) -> bool:
        file_info = self._processing_file

        if not file_info.is_valid:
            log.error(f"Encoding skipped: Invalid file for encoding: '{file_info.file_path}'")
            return False

        video_bitrate = self._target_video_bitrate()
        video_filters = list(self._video_filters().values())

        file_info.encoding_path.parent.mkdir(parents=True, exist_ok=True)
        file_info.encoding_path.unlink(missing_ok=True)

        try:
            if file_info.video_stream.shall_encode:
                file_info.passlog_path.parent.mkdir(parents=True, exist_ok=True)
                cmd = ffmpeg.get_cmd_encode_movie_pass_1(
                    file_info.file_path, file_info.passlog_path, video_bitrate, video_filters
                )
                self._run_ffmpeg(cmd, "Encoding 1st pass")

                cmd = ffmpeg.get_cmd_encode_movie_pass_2(
                    file_info.file_path,
                    file_info.encoding_path,
                    file_info.passlog_path,
                    video_bitrate,
                    video_filters,
                    file_info.audio_tracks,
                )
                self._run_ffmpeg(cmd, "Encoding 2nd pass")
            else:
                cmd = ffmpeg.get_cmd_encode_audio(file_info.file_path, file_info.encoding_path, file_info.audio_tracks)
                self._run_ffmpeg(cmd, "Encoding audio")

            log.info(f"Encoded file saved to '{file_info.encoding_path}'")
            return True

        except subprocess.CalledProcessError as e:
            log.error(f"FFmpeg encoding failed for '{file_info.file_path}' with exit code {e.returncode}: {e.stderr}")

        return False

    def _optimize(self, encoded: bool) -> bool:
        self._processing_file.processed_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ffmpeg.get_cmd_optimize(
            self._processing_file.encoding_path if encoded else self._processing_file.file_path,
            self._processing_file.processed_path,
            self._processing_file.audio_tracks,
            encoded,
        )

        try:
            self._run_ffmpeg(cmd, "Optimizing")
            log.info(f"Optimized file saved to '{self._processing_file.processed_path}'")
            return True
        except subprocess.CalledProcessError as e:
            log.error(
                f"FFmpeg optimization failed for '{self._processing_file.file_path}'"
                f" with exit code {e.returncode}: {e.stderr}"
            )
        except Exception as e:
            log.error(f"Unexpected error during FFmpeg optimization for {self._processing_file.file_path}: {e}")

        return False

    def _extract_subtitles(self) -> None:
        """Never fails the file: the video is already done and a retry would encode it all over again."""
        file_info = self._processing_file

        for stream in file_info.subtitle_tracks:
            path = file_info.subtitle_path(stream.lang)
            try:
                cmd = ffmpeg.get_cmd_extract_subtitle(
                    file_info.file_path, path, stream.index, stream.codec in SRT_CODECS
                )
                self._run_ffmpeg(cmd, "Subtitles")
                ensure_utf8(path)
                log.info(f"Subtitles saved to '{path}'")
            except Exception as e:
                log.warning(f"Extracting {stream.lang} subtitles of '{file_info.file_path}' failed: {e}")
                path.unlink(missing_ok=True)

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
