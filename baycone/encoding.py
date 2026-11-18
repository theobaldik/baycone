"""Analyzing a file with ffprobe and transcoding it with ffmpeg."""

import errno
import logging
import subprocess
import json
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from dataclasses import dataclass, field

from baycone import ffmpeg
from baycone.audio import (
    SPECIAL_DISPOSITIONS,
    UNDEFINED_LANGUAGE,
    AudioStream,
    AudioTrack,
    cap_bitrate,
    normalize_language,
    select_audio_tracks,
)
from baycone.errors import (
    EncodingError,
    InsufficientSpaceError,
    InvalidCutError,
    InvalidFileError,
    SourceChangedError,
)
from baycone.files import Signature, file_signature, fsync, fsync_dir, write_atomic
from baycone.settings import EncodingSettings, FFmpegTools
from baycone.subtitles import SRT_CODECS, SubtitleStream, ensure_utf8, language_suffix, select_subtitle_streams

log = logging.getLogger(__name__)

HDR_TRANSFERS: tuple[str, ...] = ("smpte2084", "arib-std-b67")

# h264 bitrate needed for the same quality, per kbps of the source codec
H264_EFFICIENCY: dict[str, float] = {"hevc": 1.5, "av1": 2.0, "vp9": 1.5, "mpeg4": 0.7, "mpeg2video": 0.5}

CROP_PATTERN = re.compile(r"crop=\d+:(\d+):\d+:(\d+)")

# A complete file has data within this of its declared end
TAIL_SECONDS: float = 30.0

# ffmpeg is killed once neither its progress nor its output file has moved for this long
STALL_SECONDS: float = 1800.0
STALL_CHECK_SECONDS: float = 30.0


# (step, percent) of the running FFmpeg step
type ProgressCallback = Callable[[str, float], None]


def _format_duration(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def _format_time(seconds: float) -> str:
    """A position, to the millisecond: `1:02:03.500`."""
    sign, seconds = ("-" if seconds < 0 else ""), abs(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{sign}{hours}:{minutes:02}:{seconds:06.3f}"


def _format_size(size: float) -> str:
    return f"{size / 2**30:.1f} GiB"


def _format_bitrate(kbps: int) -> str:
    return f"{kbps} kbps" if kbps > 0 else "unknown bitrate"


def _parse_number(value: object) -> float:
    """ffprobe reports unknown values as missing or 'N/A'."""
    try:
        return float(value)
    except TypeError, ValueError:
        return 0.0


def _language(stream: dict) -> str:
    tags = stream.get("tags", {})
    return normalize_language(tags.get("language") or tags.get("LANGUAGE") or tags.get("lang", ""))


def _assume_language(streams: list[AudioStream] | list[SubtitleStream], language: str) -> None:
    """Gives the streams without a language `language`, unless that is empty."""
    if not language:
        return
    for stream in streams:
        if stream.lang == UNDEFINED_LANGUAGE:
            stream.lang = normalize_language(language)


def _is_special(stream: dict) -> bool:
    disposition = stream.get("disposition", {})
    return any(disposition.get(name) for name in SPECIAL_DISPOSITIONS)


@dataclass
class VideoStream:
    """The probed video stream; `bitrate` in kbps, 0 when unknown."""

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
    def h264_bitrate(self) -> int:
        """`bitrate` converted to its h264 equivalent."""
        return int(self.bitrate * H264_EFFICIENCY.get(self.codec, 1.0))

    @property
    def is_hdr(self) -> bool:
        """HDR10 or HLG."""
        return self.color_transfer in HDR_TRANSFERS


class FileInfo:
    """The result of `Analyzer.analyze()`: the probed streams, what has to be re-encoded and the selected tracks."""

    def __init__(self, file_path: Path, **kwargs) -> None:
        self.file_path: Path = file_path

        self.is_valid: bool = kwargs.get("is_valid", False)
        self.shall_remux: bool = kwargs.get("shall_remux", False)

        self.duration: float = kwargs.get("duration", 0.0)  # seconds, 0 when unknown
        self.containers: list[str] = kwargs.get("containers", [])

        self.video_stream: VideoStream = kwargs.get("video_stream", VideoStream())
        self.audio_streams: list[AudioStream] = kwargs.get("audio_streams", [])
        self.audio_tracks: list[AudioTrack] = kwargs.get("audio_tracks", [])
        self.subtitle_streams: list[SubtitleStream] = kwargs.get("subtitle_streams", [])
        self.subtitle_tracks: list[SubtitleStream] = kwargs.get("subtitle_tracks", [])


@dataclass
class Job:
    """
    One file to process.

    :param input_file: the source, never modified
    :param output_file: where the result is published
    :param work_dir: temporary files; reusing it lets an interrupted run keep its first pass. On another filesystem
        than `output_file`, publishing copies instead of renaming
    :param name: shown in the log, the input file name when empty
    :param start: seconds on the source's timeline to cut from, None to keep its beginning. Exact when the video is
        encoded; a copied video starts at the keyframe before it (see `EncodingSettings.force_video_encode`)
    :param end: seconds on the source's timeline to cut at, None to keep its end
    """

    PASSLOG_DIR = "passlogfiles"
    ENCODED_DIR = "encoded"
    OPTIMIZED_DIR = "optimized"

    input_file: Path
    output_file: Path
    work_dir: Path
    name: str = field(default="")
    start: float | None = None
    end: float | None = None

    def __post_init__(self) -> None:
        self.name = self.name or self.input_file.name

    @property
    def passlog_dir(self) -> Path:
        return self.work_dir / self.PASSLOG_DIR

    @property
    def encoded_dir(self) -> Path:
        return self.work_dir / self.ENCODED_DIR

    @property
    def optimized_dir(self) -> Path:
        """The finished file, before it is renamed to `output_file`."""
        return self.work_dir / self.OPTIMIZED_DIR

    @property
    def passlog_path(self) -> Path:
        """A fixed name: x265 takes it inside `-x265-params`, where a ':' from a file name would end the value."""
        return self.passlog_dir / "passlog"

    @property
    def first_pass_path(self) -> Path:
        """Marks the passlog as complete and tells which file and settings it belongs to."""
        return self.passlog_dir / "pass1.json"

    @property
    def encoding_path(self) -> Path:
        return self.encoded_dir / self.output_file.name

    @property
    def optimized_path(self) -> Path:
        return self.optimized_dir / self.output_file.name

    def subtitle_path(self, lang: str) -> Path:
        """
        Gives the path subtitles of a language are extracted to.

        :param lang: ISO 639-2 code
        :return: `<output>.<lang>.srt`, with the ISO 639-1 code where the language has one
        """
        return self.output_file.with_suffix(f".{language_suffix(lang)}.srt")

    @staticmethod
    def _clear(directory: Path) -> None:
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)

    def clear_workdirs(self) -> None:
        """The passlog is not part of it: a finished first pass outlives an interrupted run."""
        self._clear(self.encoded_dir)
        self._clear(self.optimized_dir)

    def clear_passlog(self) -> None:
        """Drops a kept first pass."""
        self._clear(self.passlog_dir)


class Analyzer:
    """
    Probes files and decides, by the settings, what has to be re-encoded.

    :param settings: copied, the defaults when omitted
    :param tools: the ffmpeg and ffprobe binaries
    """

    def __init__(self, settings: EncodingSettings | None = None, tools: FFmpegTools = FFmpegTools()) -> None:
        self._settings: EncodingSettings = deepcopy(settings) if settings else EncodingSettings()
        self._tools: FFmpegTools = tools

    def _get_ffprobe_result(self, file_path: Path) -> dict | None:
        if not file_path.is_file():
            return None
        try:
            cmd = ffmpeg.get_cmd_ffprobe(file_path, tools=self._tools)
            result = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=30
            )
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            log.error(f"FFprobe command failed for {file_path}: {e.stderr}")
        except Exception as e:
            log.error(f"FFprobe failed for {file_path}: {e}")
        return None

    def _is_complete(self, file_path: Path, start_time: float, duration: float) -> bool:
        """False for an aborted copy, whose header still declares the full duration."""
        end = start_time + duration
        try:
            cmd = ffmpeg.get_cmd_ffprobe_tail(file_path, max(end - TAIL_SECONDS, start_time), tools=self._tools)
            result = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=STALL_SECONDS
            )
            packets = json.loads(result.stdout).get("packets", [])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            log.error(f"Checking the end of '{file_path}' failed: {e}")
            return False

        # AVI packets carry no pts; the latest of all, as the seek lands on the keyframe before its target
        times = [_parse_number(p.get(key)) for p in packets for key in ("pts_time", "dts_time") if key in p]
        if packets and not times:
            return True

        last = max(times, default=start_time)
        if last < end - TAIL_SECONDS:
            log.warning(
                f"'{file_path}' looks truncated: data ends near {_format_duration(last - start_time)},"
                f" the header declares {_format_duration(duration)}"
            )
            return False
        return True

    @staticmethod
    def _get_bitrate(stream: dict) -> int:
        """In kbps, 0 when unknown. Matroska only reports it in the 'BPS' tag."""
        bitrate = stream.get("bit_rate") or stream.get("tags", {}).get("BPS")
        return int(_parse_number(bitrate)) // 1000

    def _get_file_info(self, file_path: Path) -> FileInfo:
        probe_result = self._get_ffprobe_result(file_path)

        if not probe_result:
            return FileInfo(file_path=file_path, is_valid=False)

        format_info = probe_result.get("format", {})
        duration = _parse_number(format_info.get("duration"))
        if duration > 0 and not self._is_complete(file_path, _parse_number(format_info.get("start_time")), duration):
            return FileInfo(file_path=file_path, is_valid=False)

        file_info = FileInfo(file_path=file_path, is_valid=True)
        file_info.containers = format_info.get("format_name", "").split(",")
        file_info.duration = duration

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

            audio_stream.lang = _language(stream)
            audio_stream.original = bool(stream.get("disposition", {}).get("original"))
            audio_stream.special = _is_special(stream)

            file_info.audio_streams.append(audio_stream)

        subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
        for index, stream in enumerate(subtitle_streams):
            file_info.subtitle_streams.append(
                SubtitleStream(
                    index=index,
                    codec=stream.get("codec_name", ""),
                    lang=_language(stream),
                    special=_is_special(stream),
                )
            )

        return file_info

    def detect_crop(
        self, file_path: Path, video: VideoStream, start: float | None = None, end: float | None = None
    ) -> None:
        """
        Crops black bars at the top and bottom, the full width is always kept. Decodes keyframes of the whole file.

        :param file_path: the file `video` belongs to
        :param video: gets its `crop_height` and `crop_top` set
        :param start: seconds to look from, as `Job.start`
        :param end: seconds to look until, as `Job.end`
        """
        log.info("Detecting black bars...")
        try:
            result = subprocess.run(
                ffmpeg.get_cmd_cropdetect(file_path, tools=self._tools, start=start, end=end),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
                timeout=STALL_SECONDS,
            )
        except subprocess.CalledProcessError as e:
            log.warning(f"Black bar detection failed, keeping the full frame: {e.stderr.strip()[-500:]}")
            return
        except subprocess.TimeoutExpired:
            log.warning("Black bar detection took too long, keeping the full frame")
            return

        matches = CROP_PATTERN.findall(result.stderr)
        height, top = map(int, matches[-1]) if matches else (0, 0)
        if 0 < height < video.height:
            video.crop_height, video.crop_top = height, top
            log.info(f"Black bars detected, cropping to {video.width}x{height}")
        else:
            log.info("No black bars detected")

    def analyze(self, file_path: Path) -> FileInfo:
        """
        Probes a file and decides what has to be re-encoded. Nothing is raised.

        :param file_path: the file to probe
        :return: the analysis, `is_valid` False for a file that cannot be processed
        """
        file_info = self._get_file_info(file_path)
        settings = self._settings

        if not any(c in settings.allowed_containers for c in file_info.containers):
            file_info.shall_remux = True

        # An unknown bitrate cannot be checked against the limit, the encode then targets the configured one
        video = file_info.video_stream
        if (
            video.codec not in settings.allowed_video_codecs
            or not 0 < video.h264_bitrate <= settings.limit_video_bitrate
            or video.pix_fmt not in ffmpeg.VIDEO_CODECS.get(video.codec, ffmpeg.VIDEO_CODECS["h264"]).pix_fmts
            or video.is_hdr
            or video.width > settings.limit_video_width
            or video.height > settings.limit_video_height
            or settings.force_video_encode
        ):
            video.shall_encode = True

        for stream in file_info.audio_streams:
            if stream.codec not in settings.allowed_audio_codecs:
                stream.shall_encode = True
            if not 0 < stream.aac_channel_bitrate <= settings.limit_audio_channel_bitrate:
                stream.shall_encode = True

        _assume_language(file_info.audio_streams, settings.undefined_audio_language)
        _assume_language(file_info.subtitle_streams, settings.undefined_subtitle_language)
        file_info.audio_tracks = select_audio_tracks(file_info.audio_streams, settings)
        file_info.subtitle_tracks = select_subtitle_streams(file_info.subtitle_streams, settings)

        return file_info


class Encoder:
    """
    Transcodes files into streaming-friendly MP4, re-encoding only the streams that need it.

    :param settings: copied, the defaults when omitted
    :param tools: the ffmpeg and ffprobe binaries
    :param on_progress: called with the step and its percent while ffmpeg runs
    """

    def __init__(
        self,
        settings: EncodingSettings | None = None,
        tools: FFmpegTools = FFmpegTools(),
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self._settings: EncodingSettings = deepcopy(settings) if settings else EncodingSettings()
        self._tools: FFmpegTools = tools
        self._on_progress: ProgressCallback | None = on_progress
        self._analyzer: Analyzer = Analyzer(self._settings, tools)

        self._job: Job | None = None
        self._processing_file: FileInfo | None = None
        self._signature: Signature | None = None

        # Shared with the stall watchdog thread
        self._out_time: int = 0
        self._stalled: bool = False

    def process(self, job: Job, remux_only: bool = False) -> Path:
        """
        Processes one file. The input file is never modified; on failure no output is left behind.

        :param job: the file to process
        :param remux_only: copy every stream, re-encode nothing
        :return: `job.output_file`, with subtitles extracted next to it
        :raises InsufficientSpaceError: before anything is written, when the file has to wait for free space
        :raises BayconeError: when the file cannot be processed
        """
        file_path = job.input_file
        self._job = job
        self._signature = file_signature(file_path)
        self._processing_file = self._analyzer.analyze(file_path)

        try:
            if not self._processing_file.is_valid:
                raise InvalidFileError(f"Invalid file '{file_path}'")

            codec = self._settings.target_video_codec
            if codec not in ffmpeg.VIDEO_CODECS:
                raise EncodingError(f"Unsupported target_video_codec '{codec}', use one of {list(ffmpeg.VIDEO_CODECS)}")
            self._validate_cut()

            if remux_only:
                self._copy_all_streams()

            # A container mismatch or dropped audio tracks alone are fixed by the remux in `_optimize`
            shall_encode = self._processing_file.video_stream.shall_encode or any(
                t.stream.shall_encode for t in self._processing_file.audio_tracks
            )
            self._check_free_space(shall_encode)

            cut = ""
            if self._is_cut():
                cut = ", cut {} - {}".format(*map(_format_time, self._cut_bounds()))
            log.info(f"Processing '{job.name}' ({_format_duration(self._duration())}{cut})")
            # Decodes keyframes of the whole file, only worth it when the video is encoded anyway
            if self._processing_file.video_stream.shall_encode:
                self._analyzer.detect_crop(file_path, self._processing_file.video_stream, job.start, job.end)
            self._log_plan()

            if not shall_encode and self._processing_file.shall_remux:
                log.info(f"Only the container differs, remuxing '{file_path}'")

            if shall_encode and not self._encode():
                raise EncodingError(f"Encoding failed for '{file_path}'")

            if not self._optimize(shall_encode):
                raise EncodingError(f"Optimizing failed for '{file_path}'")

            # A copy that was still running, or a file replaced or deleted meanwhile
            if file_signature(file_path) != self._signature:
                raise SourceChangedError(f"'{file_path}' changed while it was processed")

            self._publish()
            self._extract_subtitles()
            return job.output_file
        finally:
            self._cleanup_workdirs()
            self._processing_file = None
            self._job = None

    def _is_cut(self) -> bool:
        return self._job.start is not None or self._job.end is not None

    def _cut_bounds(self) -> tuple[float, float]:
        """`Job.start` / `Job.end` with the file's own beginning and end in place of None."""
        job, duration = self._job, self._processing_file.duration
        return (job.start or 0.0), (duration if job.end is None else job.end)

    def _duration(self) -> float:
        """Of the output, in seconds: the cut, or the whole source; 0 when unknown."""
        start, end = self._cut_bounds()
        return end - start

    def _validate_cut(self) -> None:
        if not self._is_cut():
            return
        name, duration = self._job.input_file, self._processing_file.duration
        if not duration:
            raise InvalidCutError(f"Cannot cut '{name}', its duration is unknown")

        start, end = self._cut_bounds()
        if start < 0:
            raise InvalidCutError(f"Cut start {_format_time(start)} of '{name}' is negative")
        if start >= end:
            raise InvalidCutError(
                f"Cut start {_format_time(start)} of '{name}' is not before its end {_format_time(end)}"
            )
        if end > duration:
            raise InvalidCutError(
                f"Cut end {_format_time(end)} of '{name}' is past the end of the file, {_format_time(duration)}"
            )

    def _copy_all_streams(self) -> None:
        file_info = self._processing_file
        file_info.video_stream.shall_encode = False
        for track in file_info.audio_tracks:
            track.stream.shall_encode = False
            track.bitrate = 0

    def _required_space(self, shall_encode: bool) -> int:
        """In bytes. The encoded and the optimized file exist side by side, hence twice the output when encoding."""
        file_info = self._processing_file
        size = self._signature[0] if self._signature else 0

        # Two-pass encoding hits its bitrate, anything copied is as large as it is in the source
        if file_info.video_stream.shall_encode and file_info.duration:
            audio = sum(t.bitrate or t.stream.overall_bitrate for t in file_info.audio_tracks)
            size = int((self._target_video_bitrate() + audio) * 125 * self._duration())

        return size * 2 if shall_encode else size

    def _check_free_space(self, shall_encode: bool) -> None:
        if not self._settings.check_free_space:
            return

        work_dir = self._job.work_dir
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(work_dir)
        except OSError:
            return
        # Filesystems that cannot tell their free space report an empty disk
        if usage.total == 0:
            return

        required = self._required_space(shall_encode)
        if usage.free < required:
            raise InsufficientSpaceError(
                f"'{self._job.input_file}' needs {_format_size(required)},"
                f" '{work_dir}' has {_format_size(usage.free)} free"
            )

    def _target_video_bitrate(self) -> int:
        """`target_video_bitrate` is in kbps of the target codec, so the source is capped in its terms too."""
        efficiency = H264_EFFICIENCY.get(self._settings.target_video_codec, 1.0)
        source = int(self._processing_file.video_stream.h264_bitrate / efficiency)
        return cap_bitrate(source, self._settings.target_video_bitrate)

    def _video_filters(self) -> dict[str, str]:
        """Description -> ffmpeg filter, in the order they apply."""
        video = self._processing_file.video_stream
        filters = {}

        height = video.height
        if video.crop_height:
            height = video.crop_height
            filters[f"crop to {video.width}x{height}"] = ffmpeg.get_filter_crop(height, video.crop_top)

        # Never upscales; scaling before tone mapping leaves fewer pixels for the expensive float conversion
        box_width, box_height = self._settings.limit_video_width, self._settings.limit_video_height
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
        if not video.shall_encode and self._job.start:
            action += f" from the keyframe before {_format_time(self._job.start)} (force_video_encode cuts exactly)"
        if video.shall_encode:
            codec = self._settings.target_video_codec
            action = f"encode to {codec} {_format_bitrate(self._target_video_bitrate())}"
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

        if self._settings.target_subtitle_languages:
            languages = [s.lang for s in file_info.subtitle_tracks]
            log.info(f"Subtitles: {', '.join(languages) or 'none'} extracted")

    def _log_progress(self, process: subprocess.Popen, step: str) -> None:
        """Each update goes to `on_progress`, every ten percent to the log at DEBUG level."""
        name = self._job.name
        duration = self._duration()
        started = time.monotonic()
        percent = 0

        log.debug(f"'{name}' {step}: 0 %")
        self._report(step, 0.0)

        # Drain the pipe even without a duration, otherwise FFmpeg blocks on a full pipe
        for line in process.stdout:
            key, _, value = line.strip().partition("=")

            if key != "out_time_us" or not value.isdigit():
                continue
            self._out_time = int(value)

            if duration:
                out_percent = min(self._out_time / 10_000 / duration, 100.0)
                self._report(step, out_percent)
                if out_percent >= percent + 10:
                    percent = int(out_percent // 10) * 10
                    eta = (time.monotonic() - started) * max(100 - out_percent, 0) / out_percent
                    log.debug(f"'{name}' {step}: {percent} %, ETA {_format_duration(eta)}")

    def _report(self, step: str, percent: float) -> None:
        if self._on_progress:
            self._on_progress(step, percent)

    def _kill_when_stalled(self, process: subprocess.Popen, output_file: Path | None, done: threading.Event) -> None:
        """The output file counts too: moving the index to the front (faststart) rewrites it without any progress."""
        seen, since = None, time.monotonic()
        while not done.wait(STALL_CHECK_SECONDS):
            current = (self._out_time, file_signature(output_file) if output_file else None)
            if current != seen:
                seen, since = current, time.monotonic()
            elif time.monotonic() - since >= STALL_SECONDS:
                self._stalled = True
                process.kill()
                return

    def _run_ffmpeg(self, cmd: list[str], step: str, output_file: Path | None = None) -> None:
        started = time.monotonic()
        self._out_time, self._stalled = 0, False
        done = threading.Event()

        # A file rather than a pipe, which would block FFmpeg once full while only stdout is drained
        with tempfile.TemporaryFile(mode="w+") as stderr:
            process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr, text=True)
            threading.Thread(target=self._kill_when_stalled, args=(process, output_file, done), daemon=True).start()
            try:
                self._log_progress(process, step)
                process.wait()
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                done.set()
            if self._stalled:
                message = f"No progress for {_format_duration(STALL_SECONDS)}, FFmpeg was killed"
                raise subprocess.CalledProcessError(process.returncode, cmd, stderr=message)
            if process.returncode != 0:
                stderr.seek(0)
                raise subprocess.CalledProcessError(process.returncode, cmd, stderr=stderr.read().strip())
        log.info(f"{step} finished in {_format_duration(time.monotonic() - started)}")

    def _encode(self) -> bool:
        file_info, job = self._processing_file, self._job

        if not file_info.is_valid:
            log.error(f"Encoding skipped: Invalid file for encoding: '{file_info.file_path}'")
            return False

        codec = self._settings.target_video_codec
        video_bitrate = self._target_video_bitrate()
        video_filters = list(self._video_filters().values())

        job.encoding_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if file_info.video_stream.shall_encode:
                pass_1 = ffmpeg.get_cmd_encode_movie_pass_1(
                    file_info.file_path,
                    job.passlog_path,
                    codec,
                    video_bitrate,
                    video_filters,
                    tools=self._tools,
                    start=job.start,
                    end=job.end,
                )
                pass_2 = ffmpeg.get_cmd_encode_movie_pass_2(
                    file_info.file_path,
                    job.encoding_path,
                    job.passlog_path,
                    codec,
                    video_bitrate,
                    video_filters,
                    file_info.audio_tracks,
                    tools=self._tools,
                    start=job.start,
                    end=job.end,
                )

                resumed = self._is_first_pass_done(pass_1)
                if resumed:
                    log.info(f"First pass of '{job.name}' was finished earlier, resuming")
                else:
                    self._first_pass(pass_1)

                try:
                    self._run_ffmpeg(pass_2, "Encoding 2nd pass", job.encoding_path)
                except subprocess.CalledProcessError as e:
                    if not resumed:
                        raise
                    # The kept first pass may not fit any more, e.g. after an FFmpeg upgrade
                    log.warning(
                        f"2nd pass failed on the first pass kept from an earlier run, starting over: {e.stderr}"
                    )
                    self._first_pass(pass_1)
                    self._run_ffmpeg(pass_2, "Encoding 2nd pass", job.encoding_path)

                job.clear_passlog()
            else:
                cmd = ffmpeg.get_cmd_encode_audio(
                    file_info.file_path,
                    job.encoding_path,
                    file_info.audio_tracks,
                    tools=self._tools,
                    start=job.start,
                    end=job.end,
                )
                self._run_ffmpeg(cmd, "Encoding audio", job.encoding_path)

            log.info(f"Encoded file saved to '{job.encoding_path}'")
            return True

        except subprocess.CalledProcessError as e:
            log.error(f"FFmpeg encoding failed for '{file_info.file_path}' with exit code {e.returncode}: {e.stderr}")

        return False

    def _first_pass_key(self, cmd: list[str]) -> dict:
        """The command covers every setting the second pass has to share with the first."""
        return {"signature": list(self._signature or ()), "cmd": cmd}

    def _is_first_pass_done(self, cmd: list[str]) -> bool:
        try:
            marker = json.loads(self._job.first_pass_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return False
        return marker == self._first_pass_key(cmd)

    def _first_pass(self, cmd: list[str]) -> None:
        job = self._job
        job.clear_passlog()
        self._run_ffmpeg(cmd, "Encoding 1st pass")

        # Synced before they are declared complete, a power loss must not leave the marker with half the stats
        for stats_file in job.passlog_dir.iterdir():
            fsync(stats_file)
        write_atomic(job.first_pass_path, json.dumps(self._first_pass_key(cmd)))

    def _optimize(self, encoded: bool) -> bool:
        """Into a work directory: the output path only ever gets the finished file, see `_publish`."""
        file_info, job = self._processing_file, self._job
        job.optimized_path.parent.mkdir(parents=True, exist_ok=True)
        # The encoded file is cut already, only the source still needs it
        cmd = ffmpeg.get_cmd_optimize(
            job.encoding_path if encoded else file_info.file_path,
            job.optimized_path,
            file_info.audio_tracks,
            encoded,
            tools=self._tools,
            start=None if encoded else job.start,
            end=None if encoded else job.end,
        )

        try:
            self._run_ffmpeg(cmd, "Optimizing", job.optimized_path)
            return True
        except subprocess.CalledProcessError as e:
            log.error(
                f"FFmpeg optimization failed for '{file_info.file_path}' with exit code {e.returncode}: {e.stderr}"
            )
        except Exception as e:
            log.error(f"Unexpected error during FFmpeg optimization for {file_info.file_path}: {e}")

        return False

    def _extract_subtitles(self) -> None:
        """Never fails the file: the video is already done and a retry would encode it all over again."""
        file_info = self._processing_file

        for stream in file_info.subtitle_tracks:
            path = self._job.subtitle_path(stream.lang)
            try:
                cmd = ffmpeg.get_cmd_extract_subtitle(
                    file_info.file_path,
                    path,
                    stream.index,
                    stream.codec in SRT_CODECS,
                    tools=self._tools,
                    start=self._job.start,
                    end=self._job.end,
                )
                self._run_ffmpeg(cmd, "Subtitles", path)
                ensure_utf8(path)
                log.info(f"Subtitles saved to '{path}'")
            except Exception as e:
                log.warning(f"Extracting {stream.lang} subtitles of '{file_info.file_path}' failed: {e}")
                path.unlink(missing_ok=True)

    def _publish(self) -> None:
        """Synced before the rename, so a caller archiving the input does so only for an output safely on disk."""
        source_path = self._job.optimized_path
        target_path = self._job.output_file

        fsync(source_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_path.replace(target_path)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            # Another filesystem: a synced copy is renamed into place, so the output is never seen half-written
            temp_path = target_path.with_name(f".{target_path.name}.tmp")
            shutil.copyfile(source_path, temp_path)
            fsync(temp_path)
            temp_path.replace(target_path)
            source_path.unlink()
        fsync_dir(target_path.parent)
        log.info(f"Processed file saved to '{target_path}'")

    def _cleanup_workdirs(self) -> None:
        """Must not raise: it runs in a `finally` and would turn a finished file into a failed one."""
        try:
            self._job.clear_workdirs()
        except OSError as e:
            log.warning(f"Work directories could not be cleaned up: {e}")
