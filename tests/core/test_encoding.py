import errno
import itertools
import json
import subprocess
import threading
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone import ffmpeg
from baycone.audio import AudioStream, AudioTrack
from baycone.encoding import Analyzer, Encoder, FileInfo, Job, VideoStream
from baycone.errors import BayconeError, InsufficientSpaceError
from baycone.files import file_signature
from baycone.settings import EncodingSettings

DiskUsage = namedtuple("DiskUsage", "total used free")


def make_ffprobe_output(
    container: str = "mp4",
    duration: float | str = 100.0,
    video_codec: str = "h264",
    video_bitrate: int = 2_000_000,
    video_width: int = 1920,
    video_height: int = 1080,
    video_pix_fmt: str = "yuv420p",
    video_color_transfer: str = "bt709",
    audio_codec: str = "aac",
    audio_bitrate: int = 128_000,
    audio_channels: int = 2,
    audio_lang: str = "eng",
    audio_disposition: dict[str, int] | None = None,
    subtitles: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "format": {"format_name": container, "duration": str(duration)},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": video_codec,
                    "bit_rate": str(video_bitrate),
                    "width": video_width,
                    "height": video_height,
                    "pix_fmt": video_pix_fmt,
                    "color_transfer": video_color_transfer,
                },
                {
                    "codec_type": "audio",
                    "codec_name": audio_codec,
                    "bit_rate": str(audio_bitrate),
                    "channels": audio_channels,
                    "tags": {"language": audio_lang},
                    "disposition": audio_disposition or {},
                },
                *[{"codec_type": "subtitle", **subtitle} for subtitle in subtitles or []],
            ],
        }
    )


def make_popen_mock(returncode: int = 0, lines: list[str] | None = None) -> MagicMock:
    process = MagicMock()
    process.stdout = iter(lines or [])
    process.returncode = returncode
    return process


@pytest.fixture(autouse=True)
def settings(monkeypatch) -> EncodingSettings:
    """What every `Encoder()` and `Analyzer()` of a test uses, so a test can change it at any point."""
    shared = EncodingSettings()
    monkeypatch.setattr("baycone.encoding.EncodingSettings", lambda: shared)
    return shared


def job_for(file_path: Path) -> Job:
    return Job(
        input_file=file_path,
        output_file=file_path.parent / "_baycone_processed" / file_path.with_suffix(".mp4").name,
        work_dir=file_path.parent / ".baycone",
    )


@pytest.fixture
def media_file(tmp_path) -> Path:
    video = tmp_path / "movie.mp4"
    video.touch()
    return video


@pytest.fixture
def job(media_file) -> Job:
    return job_for(media_file)


@pytest.fixture
def work_dirs(job) -> Job:
    """The job with its work and output directories in place."""
    for directory in (job.passlog_dir, job.encoded_dir, job.optimized_dir, job.output_file.parent):
        directory.mkdir(parents=True, exist_ok=True)
    return job


def analyze(file_path: Path, ffprobe_output: str, method: str = "analyze") -> FileInfo:
    with (
        patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout=ffprobe_output)),
        patch.object(Analyzer, "_is_complete", return_value=True),
    ):
        return getattr(Analyzer(), method)(file_path)


def encoder_for(file_info: FileInfo) -> Encoder:
    encoder = Encoder()
    encoder._job = job_for(file_info.file_path)
    encoder._processing_file = file_info
    return encoder


class TestJobPaths:
    def test_passlog_path_is_independent_of_the_file_name(self, tmp_path):
        """x265 gets it inside `-x265-params`, where the ':' of a name like this one would cut the value short."""
        job = job_for(tmp_path / "Mission: Impossible.mkv")
        assert job.passlog_path == tmp_path / ".baycone" / "passlogfiles" / "passlog"

    def test_work_files_are_named_after_the_output(self, tmp_path):
        job = Job(tmp_path / "movie.mkv", tmp_path / "out" / "film.mp4", tmp_path / "work")
        assert job.encoding_path == tmp_path / "work" / "encoded" / "film.mp4"
        assert job.optimized_path == tmp_path / "work" / "optimized" / "film.mp4"

    def test_name_defaults_to_the_input_file_name(self, tmp_path):
        assert job_for(tmp_path / "movie.mkv").name == "movie.mkv"

    def test_subtitles_go_next_to_the_output_with_a_two_letter_language(self, tmp_path):
        job = job_for(tmp_path / "movie.mkv")
        assert job.subtitle_path("ces") == job.output_file.with_suffix(".cs.srt")


class TestGetFfprobeResult:
    def test_returns_parsed_dict_on_success(self, media_file):
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout=make_ffprobe_output())):
            result = Analyzer()._get_ffprobe_result(media_file)
        assert isinstance(result, dict)
        assert "streams" in result

    def test_returns_none_for_missing_file(self, tmp_path):
        assert Analyzer()._get_ffprobe_result(tmp_path / "nonexistent.mp4") is None

    def test_returns_none_on_subprocess_error(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            assert Analyzer()._get_ffprobe_result(media_file) is None

    def test_returns_none_on_timeout(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            assert Analyzer()._get_ffprobe_result(media_file) is None

    def test_returns_none_on_invalid_json(self, media_file):
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout="not json")):
            assert Analyzer()._get_ffprobe_result(media_file) is None


class TestIsComplete:
    """Declared duration 3600 s; a complete file has data in its last 30 s."""

    def _check(self, media_file: Path, packets: list[dict], start_time: float = 0.0) -> tuple[bool, list[str]]:
        stdout = json.dumps({"packets": packets})
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout=stdout)) as run:
            complete = Analyzer()._is_complete(media_file, start_time, 3600.0)
        return complete, run.call_args.args[0]

    def test_complete_when_data_reaches_the_end(self, media_file):
        assert self._check(media_file, [{"pts_time": "3570.0"}, {"pts_time": "3599.9"}])[0] is True

    def test_truncated_when_data_ends_early(self, media_file):
        assert self._check(media_file, [{"pts_time": "1799.0"}, {"pts_time": "1800.0"}])[0] is False

    def test_truncated_just_short_of_the_tail(self, media_file):
        assert self._check(media_file, [{"pts_time": "3569.9"}])[0] is False

    def test_truncated_without_packets(self, media_file):
        assert self._check(media_file, [])[0] is False

    def test_latest_packet_counts_not_the_first(self, media_file):
        """The seek lands on the keyframe before its target, with a long GOP far before it."""
        assert self._check(media_file, [{"pts_time": "0.0"}, {"pts_time": "3599.9"}])[0] is True

    def test_packets_are_not_expected_in_order(self, media_file):
        assert self._check(media_file, [{"pts_time": "3599.9"}, {"pts_time": "3590.0"}])[0] is True

    def test_dts_is_used_where_there_is_no_pts(self, media_file):
        """AVI: every complete file used to be rejected."""
        assert self._check(media_file, [{"dts_time": "3570.0"}, {"dts_time": "3599.9"}])[0] is True

    def test_truncated_by_dts(self, media_file):
        assert self._check(media_file, [{"dts_time": "1800.0"}])[0] is False

    def test_packets_without_any_timestamp_cannot_be_judged(self, media_file):
        assert self._check(media_file, [{}, {}])[0] is True

    def test_truncated_when_probe_fails(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            assert Analyzer()._is_complete(media_file, 0.0, 3600.0) is False

    def test_truncated_when_probe_hangs(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 1800)):
            assert Analyzer()._is_complete(media_file, 0.0, 3600.0) is False

    def test_seeks_relative_to_start_time(self, media_file):
        complete, cmd = self._check(media_file, [{"pts_time": "4599.9"}], start_time=1000.0)
        assert complete is True
        assert "4570.000%" in cmd

    def test_early_data_with_start_time_is_truncated(self, media_file):
        assert self._check(media_file, [{"pts_time": "3599.9"}], start_time=1000.0)[0] is False

    def test_short_file_is_read_from_its_start(self, media_file):
        with patch(
            "baycone.encoding.subprocess.run", return_value=MagicMock(stdout='{"packets": [{"pts_time": "9.9"}]}')
        ) as run:
            assert Analyzer()._is_complete(media_file, 0.0, 10.0) is True
        assert "0.000%" in run.call_args.args[0]


class TestGetFileInfo:
    def _info(self, media_file: Path, **kwargs) -> FileInfo:
        return analyze(media_file, make_ffprobe_output(**kwargs), method="_get_file_info")

    def test_is_invalid_when_truncated(self, media_file):
        with (
            patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout=make_ffprobe_output())),
            patch.object(Analyzer, "_is_complete", return_value=False),
        ):
            assert Analyzer()._get_file_info(media_file).is_valid is False

    def test_unknown_duration_skips_the_end_check(self, media_file):
        output = make_ffprobe_output(duration="N/A")
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout=output)) as run:
            assert Analyzer()._get_file_info(media_file).is_valid is True
        assert run.call_count == 1

    def test_is_valid_on_successful_probe(self, media_file):
        assert self._info(media_file).is_valid is True

    def test_is_invalid_when_ffprobe_fails(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            assert Analyzer()._get_file_info(media_file).is_valid is False

    def test_duration_defaults_to_zero_when_invalid(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            assert Analyzer()._get_file_info(media_file).duration == 0

    def test_containers_parsed(self, media_file):
        info = self._info(media_file, container="mov,mp4,m4a")
        assert "mp4" in info.containers
        assert "mov" in info.containers

    def test_duration_parsed(self, media_file):
        assert self._info(media_file, duration=1234.9).duration == 1234

    def test_video_codec_extracted(self, media_file):
        assert self._info(media_file, video_codec="hevc").video_stream.codec == "hevc"

    def test_cover_art_is_not_the_video(self, media_file):
        output = json.loads(make_ffprobe_output(video_codec="hevc"))
        cover = {"codec_type": "video", "codec_name": "png", "width": 300, "height": 300}
        output["streams"].insert(0, cover | {"disposition": {"attached_pic": 1}})
        info = analyze(media_file, json.dumps(output), method="_get_file_info")
        assert (info.video_stream.codec, info.video_stream.width) == ("hevc", 1920)

    def test_video_bitrate_converted_to_kbps(self, media_file):
        assert self._info(media_file, video_bitrate=2_000_000).video_stream.bitrate == 2000

    def test_audio_codec_extracted(self, media_file):
        assert self._info(media_file, audio_codec="ac3").audio_streams[0].codec == "ac3"

    def test_audio_channels_extracted(self, media_file):
        assert self._info(media_file, audio_channels=6).audio_streams[0].channels == 6

    def test_audio_channel_bitrate_calculated(self, media_file):
        info = self._info(media_file, audio_bitrate=192_000, audio_channels=2)
        assert info.audio_streams[0].channel_bitrate == 96  # 192000 // 1000 // 2

    def test_audio_language_normalized_to_terminology_code(self, media_file):
        assert self._info(media_file, audio_lang="cze").audio_streams[0].lang == "ces"

    def test_missing_audio_language_is_undefined(self, media_file):
        assert self._info(media_file, audio_lang="").audio_streams[0].lang == "und"

    def test_audio_streams_are_indexed_among_audio_only(self, media_file):
        payload = json.dumps(
            {
                "format": {"format_name": "mp4", "duration": "100.0"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264"},
                    {"codec_type": "audio", "codec_name": "aac", "channels": 2},
                    {"codec_type": "subtitle", "codec_name": "mov_text"},
                    {"codec_type": "audio", "codec_name": "aac", "channels": 6},
                ],
            }
        )
        info = analyze(media_file, payload, method="_get_file_info")
        assert [s.index for s in info.audio_streams] == [0, 1]

    def test_original_disposition_extracted(self, media_file):
        assert self._info(media_file, audio_disposition={"original": 1}).audio_streams[0].original is True

    @pytest.mark.parametrize("disposition", ["comment", "visual_impaired", "descriptions", "hearing_impaired"])
    def test_special_disposition_extracted(self, media_file, disposition):
        assert self._info(media_file, audio_disposition={disposition: 1}).audio_streams[0].special is True

    def test_dub_is_not_special(self, media_file):
        assert self._info(media_file, audio_disposition={"dub": 1, "default": 1}).audio_streams[0].special is False

    def test_video_geometry_and_color_extracted(self, media_file):
        video = self._info(
            media_file,
            video_width=3840,
            video_height=2160,
            video_pix_fmt="yuv420p10le",
            video_color_transfer="smpte2084",
        ).video_stream
        assert (video.width, video.height, video.pix_fmt, video.color_transfer) == (
            3840,
            2160,
            "yuv420p10le",
            "smpte2084",
        )
        assert video.is_hdr is True

    def test_sdr_is_not_hdr(self, media_file):
        assert self._info(media_file).video_stream.is_hdr is False

    def test_unknown_values_default_to_zero(self, media_file):
        payload = json.dumps(
            {
                "format": {"format_name": "mp4", "duration": "N/A"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "bit_rate": "N/A"},
                    {"codec_type": "audio", "codec_name": "aac", "channels": 2},
                ],
            }
        )
        info = analyze(media_file, payload, method="_get_file_info")
        assert info.duration == 0
        assert info.video_stream.bitrate == 0
        assert info.audio_streams[0].channel_bitrate == 0

    def test_matroska_bitrate_read_from_bps_tag(self, media_file):
        payload = json.dumps(
            {
                "format": {"format_name": "matroska,webm", "duration": "100.0"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "tags": {"BPS": "3000000"}}],
            }
        )
        assert analyze(media_file, payload, method="_get_file_info").video_stream.bitrate == 3000

    def test_no_audio_streams_on_video_only_file(self, media_file):
        payload = json.dumps(
            {
                "format": {"format_name": "mp4", "duration": "100.0"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "bit_rate": "1000000"}],
            }
        )
        assert analyze(media_file, payload, method="_get_file_info").audio_streams == []


class TestAnalyze:
    def _analyze(self, media_file: Path, **kwargs) -> FileInfo:
        return analyze(media_file, make_ffprobe_output(**kwargs))

    def test_no_encode_needed_when_all_within_limits(self, media_file):
        info = self._analyze(media_file)
        assert info.shall_remux is False
        assert info.video_stream.shall_encode is False
        assert all(not s.shall_encode for s in info.audio_streams)

    def test_remux_needed_wrong_container(self, media_file):
        info = self._analyze(media_file, container="avi")
        assert info.shall_remux is True
        assert info.video_stream.shall_encode is False

    def test_encode_needed_wrong_video_codec(self, media_file):
        assert self._analyze(media_file, video_codec="hevc").video_stream.shall_encode is True

    def test_encode_needed_video_bitrate_over_limit(self, media_file, settings):
        settings.limit_video_bitrate = 1000
        assert self._analyze(media_file, video_bitrate=2_000_000).video_stream.shall_encode is True

    def test_encode_needed_wrong_audio_codec(self, media_file):
        assert self._analyze(media_file, audio_codec="ac3").audio_streams[0].shall_encode is True

    def test_audio_codec_check_is_not_a_substring_match(self, media_file, settings):
        """Regression: the check used to be `codec not in target_audio_codec` (a substring test on a string)."""
        settings.allowed_audio_codecs = ["aac"]
        assert self._analyze(media_file, audio_codec="aa").audio_streams[0].shall_encode is True

    def test_audio_codec_check_uses_allowed_audio_codecs(self, media_file, settings):
        settings.allowed_audio_codecs = ["aac", "ac3"]
        assert self._analyze(media_file, audio_codec="ac3").audio_streams[0].shall_encode is False

    def test_encode_needed_audio_bitrate_over_limit(self, media_file, settings):
        settings.limit_audio_channel_bitrate = 64
        info = self._analyze(media_file, audio_bitrate=256_000, audio_channels=2)
        assert info.audio_streams[0].shall_encode is True

    def test_selects_audio_tracks(self, media_file):
        info = self._analyze(media_file)
        assert [t.stream for t in info.audio_tracks] == info.audio_streams

    def test_selected_track_needing_encode_gets_a_bitrate(self, media_file, settings):
        settings.target_audio_channel_bitrate = 64
        info = self._analyze(media_file, audio_codec="eac3", audio_bitrate=448_000, audio_channels=6)
        assert info.audio_tracks[0].bitrate == 384

    @pytest.mark.parametrize("video_bitrate,expected", [(2_000_000, True), (1_500_000, False)])
    def test_video_limit_compares_the_h264_equivalent(self, media_file, settings, video_bitrate, expected):
        settings.allowed_video_codecs = ["h264", "hevc"]
        settings.limit_video_bitrate = 2800
        info = self._analyze(media_file, video_codec="hevc", video_bitrate=video_bitrate)
        assert info.video_stream.shall_encode is expected

    def test_less_efficient_video_codec_within_limit_as_h264_equivalent(self, media_file, settings):
        settings.allowed_video_codecs = ["h264", "mpeg2video"]
        settings.limit_video_bitrate = 2800
        info = self._analyze(media_file, video_codec="mpeg2video", video_bitrate=5_000_000)
        assert info.video_stream.shall_encode is False

    def test_audio_limit_compares_the_aac_equivalent(self, media_file, settings):
        settings.allowed_audio_codecs = ["aac", "opus"]
        settings.limit_audio_channel_bitrate = 96
        info = self._analyze(media_file, audio_codec="opus", audio_bitrate=160_000, audio_channels=2)
        assert info.audio_streams[0].shall_encode is True


class TestEncoderProcess:
    def _file_info(self, media_file: Path, **kwargs) -> FileInfo:
        return FileInfo(
            media_file,
            is_valid=True,
            duration=100,
            containers=["mp4"],
            video_stream=VideoStream(codec="h264", bitrate=2000, shall_encode=kwargs.get("video_shall_encode", False)),
            audio_tracks=[
                AudioTrack(AudioStream(codec="aac", channels=2, shall_encode=kwargs.get("audio_shall_encode", False)))
            ],
            shall_remux=kwargs.get("shall_remux", False),
        )

    def _process(self, encoder: Encoder, file_info: FileInfo, media_file: Path, **mocks):
        """`result` is True for a published file, False for a `BayconeError`."""
        with (
            patch.object(Analyzer, "analyze", return_value=file_info),
            patch.object(encoder, "_encode", **mocks.get("encode", {"return_value": True})) as mock_encode,
            patch.object(encoder, "_optimize", **mocks.get("optimize", {"return_value": True})) as mock_optimize,
            patch.object(encoder, "_publish", **mocks.get("publish", {})) as mock_publish,
            patch.object(encoder, "_cleanup_workdirs") as mock_cleanup,
            patch.object(Analyzer, "detect_crop", **mocks.get("detect_crop", {})),
        ):
            try:
                result = encoder.process(job_for(media_file)) == job_for(media_file).output_file
            except InsufficientSpaceError:
                raise
            except BayconeError:
                result = False
        return result, mock_encode, mock_optimize, mock_publish, mock_cleanup

    def test_detects_black_bars_only_when_the_video_is_encoded(self, media_file, work_dirs):
        detect = MagicMock()
        self._process(Encoder(), self._file_info(media_file), media_file, detect_crop={"new": detect})
        detect.assert_not_called()

    def test_detects_black_bars_when_the_video_is_encoded(self, media_file, work_dirs):
        detect = MagicMock()
        info = self._file_info(media_file, video_shall_encode=True)
        self._process(Encoder(), info, media_file, detect_crop={"new": detect})
        detect.assert_called_once_with(media_file, info.video_stream)

    def test_logs_a_headline_before_anything_else(self, media_file, work_dirs, caplog):
        caplog.set_level("INFO")
        self._process(Encoder(), self._file_info(media_file), media_file)
        assert caplog.messages[0] == "Processing 'movie.mp4' (0:01:40)"

    def test_invalid_file_is_not_described(self, media_file, work_dirs, caplog):
        caplog.set_level("INFO")
        info = FileInfo(media_file, is_valid=False)
        self._process(Encoder(), info, media_file, encode={"return_value": False})
        assert not [m for m in caplog.messages if m.startswith(("Processing '", "Video:", "Audio:"))]

    def test_unsupported_target_video_codec_is_never_encoded(self, media_file, work_dirs, settings):
        settings.target_video_codec = "vp9"
        info = self._file_info(media_file, video_shall_encode=True)
        result, mock_encode, _, mock_publish, _ = self._process(Encoder(), info, media_file)
        assert result is False
        mock_encode.assert_not_called()
        mock_publish.assert_not_called()

    def test_invalid_file_is_never_archived(self, media_file, work_dirs):
        info = FileInfo(media_file, is_valid=False)
        result, encode, optimize, publish, cleanup = self._process(Encoder(), info, media_file)
        assert result is False
        encode.assert_not_called()
        optimize.assert_not_called()
        publish.assert_not_called()
        cleanup.assert_called_once()

    def test_skips_encode_when_not_needed(self, media_file, work_dirs):
        encoder = Encoder()
        result, encode, optimize, publish, _ = self._process(encoder, self._file_info(media_file), media_file)
        assert result is True
        encode.assert_not_called()
        optimize.assert_called_once_with(False)
        publish.assert_called_once()

    def test_wrong_container_is_only_remuxed(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, shall_remux=True)
        _, encode, optimize, _, _ = self._process(encoder, info, media_file)
        encode.assert_not_called()
        optimize.assert_called_once_with(False)

    def test_calls_encode_when_needed(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        _, encode, optimize, publish, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()
        optimize.assert_called_once_with(True)
        publish.assert_called_once()

    def test_encode_triggered_by_video_stream(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        _, encode, _, _, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()

    def test_encode_triggered_by_audio_stream(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, audio_shall_encode=True)
        _, encode, _, _, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()

    def test_dropped_audio_streams_never_trigger_encode(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file)
        info.audio_streams = [AudioStream(codec="dts", shall_encode=True)]
        _, encode, optimize, _, _ = self._process(encoder, info, media_file)
        encode.assert_not_called()
        optimize.assert_called_once_with(False)

    def test_source_changed_meanwhile_is_neither_published_nor_archived(self, media_file, work_dirs):
        """A copy that was still running would otherwise end up archived as the original."""
        publish = MagicMock()
        result, _, _, publish, cleanup = self._process(
            Encoder(),
            self._file_info(media_file),
            media_file,
            optimize={"side_effect": lambda encoded: bool(media_file.write_text("more data"))},
            publish={"new": publish},
        )
        assert result is False
        publish.assert_not_called()
        publish.assert_not_called()
        cleanup.assert_called_once()

    def test_source_deleted_meanwhile_is_not_published(self, media_file, work_dirs):
        publish = MagicMock()
        result, _, _, publish, _ = self._process(
            Encoder(),
            self._file_info(media_file),
            media_file,
            optimize={"side_effect": lambda encoded: media_file.unlink() or True},
            publish={"new": publish},
        )
        assert result is False
        publish.assert_not_called()
        publish.assert_not_called()

    def test_failed_publish_raises(self, media_file, work_dirs):
        """A caller archiving the original must never get to it."""
        encoder = Encoder()
        with (
            patch.object(Analyzer, "analyze", return_value=self._file_info(media_file)),
            patch.object(encoder, "_optimize", return_value=True),
            patch.object(encoder, "_publish", side_effect=OSError("I/O error")),
            pytest.raises(OSError),
        ):
            encoder.process(job_for(media_file))

    def test_remux_only_copies_every_stream(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True, audio_shall_encode=True)
        info.audio_tracks[0].bitrate = 128
        with (
            patch.object(Analyzer, "analyze", return_value=info),
            patch.object(encoder, "_encode") as encode,
            patch.object(encoder, "_optimize", return_value=True) as optimize,
            patch.object(encoder, "_publish"),
        ):
            encoder.process(job_for(media_file), remux_only=True)
        encode.assert_not_called()
        optimize.assert_called_once_with(False)
        assert info.audio_tracks[0].bitrate == 0

    def test_returns_the_output_file(self, media_file, work_dirs):
        encoder = Encoder()
        with (
            patch.object(Analyzer, "analyze", return_value=self._file_info(media_file)),
            patch.object(encoder, "_optimize", return_value=True),
            patch.object(encoder, "_publish"),
        ):
            assert encoder.process(work_dirs) == work_dirs.output_file

    def test_waits_for_free_space_before_doing_anything(self, media_file, work_dirs):
        media_file.write_bytes(b"x" * 1000)
        detect = MagicMock()
        with patch("baycone.encoding.shutil.disk_usage", return_value=DiskUsage(10_000, 9_500, 500)):
            with pytest.raises(InsufficientSpaceError):
                self._process(Encoder(), self._file_info(media_file), media_file, detect_crop={"new": detect})
        detect.assert_not_called()
        assert media_file.is_file()

    def test_aborts_without_archiving_when_encode_fails(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        result, _, optimize, publish, cleanup = self._process(encoder, info, media_file, encode={"return_value": False})
        assert result is False
        optimize.assert_not_called()
        publish.assert_not_called()
        cleanup.assert_called_once()

    def test_aborts_without_archiving_when_optimize_fails(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        result, _, _, publish, cleanup = self._process(encoder, info, media_file, optimize={"return_value": False})
        assert result is False
        publish.assert_not_called()
        cleanup.assert_called_once()

    def test_cleans_up_workdirs_even_when_a_step_raises(self, media_file, work_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        with (
            patch.object(Analyzer, "analyze", return_value=info),
            patch.object(Analyzer, "detect_crop"),
            patch.object(encoder, "_encode", side_effect=RuntimeError("boom")),
            patch.object(encoder, "_cleanup_workdirs") as mock_cleanup,
        ):
            with pytest.raises(RuntimeError):
                encoder.process(job_for(media_file))
        mock_cleanup.assert_called_once()

    def test_resets_processing_file_after_completion(self, media_file, work_dirs):
        encoder = Encoder()
        self._process(encoder, self._file_info(media_file), media_file)
        assert encoder._processing_file is None


class TestSubtitles:
    SUBTITLES = [
        {"codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
        {"codec_name": "subrip", "tags": {"language": "cze"}},
        {"codec_name": "ass", "tags": {"LANGUAGE": "eng"}, "disposition": {"comment": 1}},
        {"codec_name": "subrip", "tags": {"language": "eng"}},
    ]

    def test_analyzer_selects_configured_text_streams(self, media_file, settings):
        settings.target_subtitle_languages = ["eng", "cze"]
        info = analyze(media_file, make_ffprobe_output(subtitles=self.SUBTITLES))
        assert [s.index for s in info.subtitle_streams] == [0, 1, 2, 3]
        assert [(s.index, s.lang) for s in info.subtitle_tracks] == [(3, "eng"), (1, "ces")]

    def test_analyzer_selects_nothing_by_default(self, media_file, settings):
        info = analyze(media_file, make_ffprobe_output(subtitles=self.SUBTITLES))
        assert info.subtitle_tracks == []

    def _extract(self, media_file, settings, run_ffmpeg: MagicMock) -> Encoder:
        settings.target_subtitle_languages = ["eng", "cze"]
        info = analyze(media_file, make_ffprobe_output(subtitles=self.SUBTITLES))
        encoder = encoder_for(info)
        encoder._job.output_file.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(encoder, "_run_ffmpeg", run_ffmpeg):
            encoder._extract_subtitles()
        return encoder

    def test_extracts_each_track_next_to_the_processed_file(self, media_file, settings):
        run_ffmpeg = MagicMock(side_effect=lambda cmd, step: Path(cmd[-1]).write_text("text"))
        encoder = self._extract(media_file, settings, run_ffmpeg)
        info = encoder._job
        cmds = [call.args[0] for call in run_ffmpeg.call_args_list]
        assert [cmd[cmd.index("-map") + 1] for cmd in cmds] == ["0:s:3", "0:s:1"]
        assert [Path(cmd[-1]) for cmd in cmds] == [info.subtitle_path("eng"), info.subtitle_path("ces")]
        assert info.subtitle_path("ces").name == "movie.cs.srt"

    def test_failure_is_not_fatal_and_leaves_no_partial_file(self, media_file, settings, caplog):
        def fail(cmd, step):
            Path(cmd[-1]).write_text("partial")
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")

        encoder = self._extract(media_file, settings, MagicMock(side_effect=fail))
        assert not encoder._job.subtitle_path("eng").exists()
        assert "failed" in caplog.text

    def test_process_extracts_after_publishing(self, media_file, work_dirs):
        encoder = Encoder()
        calls = []
        with (
            patch.object(Analyzer, "analyze", return_value=FileInfo(media_file, is_valid=True, duration=1)),
            patch.object(encoder, "_optimize", return_value=True),
            patch.object(encoder, "_publish", side_effect=lambda: calls.append("publish")),
            patch.object(encoder, "_extract_subtitles", side_effect=lambda: calls.append("extract")),
            patch.object(encoder, "_cleanup_workdirs"),
        ):
            assert encoder.process(job_for(media_file))
        assert calls == ["publish", "extract"]


class TestVideoEncodeDecision:
    def _video(self, media_file: Path, **kwargs) -> VideoStream:
        return analyze(media_file, make_ffprobe_output(**kwargs)).video_stream

    def test_copied_when_within_limits(self, media_file):
        assert self._video(media_file).shall_encode is False

    def test_encoded_when_bitrate_is_unknown(self, media_file):
        assert self._video(media_file, video_bitrate=0).shall_encode is True

    def test_encoded_when_10_bit(self, media_file):
        assert self._video(media_file, video_pix_fmt="yuv420p10le").shall_encode is True

    def test_10_bit_hevc_copied_when_allowed(self, media_file, settings):
        settings.allowed_video_codecs = ["h264", "hevc"]
        video = self._video(media_file, video_codec="hevc", video_bitrate=1_500_000, video_pix_fmt="yuv420p10le")
        assert video.shall_encode is False

    def test_hdr_hevc_encoded_even_when_allowed(self, media_file, settings):
        settings.allowed_video_codecs = ["h264", "hevc"]
        video = self._video(
            media_file, video_codec="hevc", video_pix_fmt="yuv420p10le", video_color_transfer="smpte2084"
        )
        assert video.shall_encode is True

    def test_encoded_when_taller_than_the_limit(self, media_file):
        assert self._video(media_file, video_width=3840, video_height=2160).shall_encode is True

    def test_encoded_when_wider_than_the_box(self, media_file):
        assert self._video(media_file, video_width=2560, video_height=1080).shall_encode is True

    def test_limit_follows_the_config(self, media_file, settings):
        settings.limit_video_height = 720
        assert self._video(media_file).shall_encode is True

    def test_audio_encoded_when_bitrate_is_unknown(self, media_file):
        info = analyze(media_file, make_ffprobe_output(audio_bitrate=0))
        assert info.audio_streams[0].shall_encode is True


class TestDetectCrop:
    STDERR = "[Parsed_cropdetect_0] x1:0 x2:1919 y1:140 y2:939 w:1920 h:800 x:0 y:140 crop=1920:800:0:140\n"

    def _video(self, media_file: Path, stderr: str = STDERR, **kwargs) -> VideoStream:
        video = VideoStream(width=1920, height=1080)
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stderr=stderr), **kwargs):
            Analyzer().detect_crop(media_file, video)
        return video

    def test_crops_letterbox(self, media_file):
        video = self._video(media_file)
        assert (video.crop_height, video.crop_top) == (800, 140)

    def test_uses_the_last_report(self, media_file):
        video = self._video(media_file, "crop=1920:700:0:190\n" + self.STDERR)
        assert video.crop_height == 800

    def test_full_height_is_no_crop(self, media_file):
        assert self._video(media_file, "crop=1920:1080:0:0\n").crop_height == 0

    def test_no_report_is_no_crop(self, media_file):
        assert self._video(media_file, "").crop_height == 0

    def test_timeout_is_no_crop(self, media_file):
        video = VideoStream(width=1920, height=1080)
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1800)):
            Analyzer().detect_crop(media_file, video)
        assert video.crop_height == 0

    def test_failure_is_no_crop(self, media_file):
        video = self._video(media_file, side_effect=subprocess.CalledProcessError(1, "ffmpeg", stderr="boom"))
        assert video.crop_height == 0


class TestVideoFilters:
    def _filters(self, media_file: Path, **kwargs) -> list[str]:
        encoder = encoder_for(FileInfo(media_file, video_stream=VideoStream(**kwargs)))
        return list(encoder._video_filters().values())

    def test_none_for_plain_1080p(self, media_file):
        assert self._filters(media_file, width=1920, height=1080) == []

    def test_crop_only(self, media_file):
        assert self._filters(media_file, width=1920, height=1080, crop_height=800, crop_top=140) == [
            "crop=iw:800:0:140"
        ]

    def test_scales_down_to_the_box(self, media_file):
        assert self._filters(media_file, width=3840, height=2160) == [
            "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2"
        ]

    def test_never_upscales(self, media_file):
        assert self._filters(media_file, width=1280, height=720) == []

    def test_crop_comes_before_scale(self, media_file):
        filters = self._filters(media_file, width=3840, height=2160, crop_height=1600, crop_top=280)
        assert [f.split("=")[0] for f in filters] == ["crop", "scale"]

    def test_tone_maps_hdr_last(self, media_file):
        filters = self._filters(media_file, width=3840, height=2160, color_transfer="smpte2084")
        assert filters[-1] == ffmpeg.get_filter_tonemap("smpte2084")


class TestEncoderEncode:
    def _encoder(self, media_file: Path, **kwargs) -> Encoder:
        encoder = encoder_for(
            FileInfo(
                media_file,
                is_valid=kwargs.get("is_valid", True),
                duration=100,
                video_stream=VideoStream(
                    codec=kwargs.get("video_codec", "h264"),
                    bitrate=kwargs.get("video_bitrate", 5000),
                    shall_encode=kwargs.get("encode_video", True),
                ),
                audio_tracks=kwargs.get(
                    "audio_tracks", [AudioTrack(AudioStream(codec="aac", channels=2), bitrate=128)]
                ),
            )
        )
        return encoder

    def test_returns_true_on_success(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]):
            assert encoder._encode() is True

    def test_runs_two_ffmpeg_passes(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch(
            "baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]
        ) as mock_popen:
            encoder._encode()
        assert mock_popen.call_count == 2

    def test_returns_false_for_invalid_file(self, media_file, work_dirs):
        encoder = self._encoder(media_file, is_valid=False)
        with patch("baycone.encoding.subprocess.Popen") as mock_popen:
            assert encoder._encode() is False
        mock_popen.assert_not_called()

    def test_encodes_video_without_audio_tracks(self, media_file, work_dirs):
        encoder = self._encoder(media_file, audio_tracks=[])
        with patch(
            "baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]
        ) as mock_popen:
            assert encoder._encode() is True
        assert mock_popen.call_count == 2

    def test_returns_false_when_first_pass_fails(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock(returncode=1)):
            assert encoder._encode() is False

    def test_returns_false_when_second_pass_fails(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        passes = [make_popen_mock(), make_popen_mock(returncode=1)]
        with patch("baycone.encoding.subprocess.Popen", side_effect=passes):
            assert encoder._encode() is False

    def test_video_bitrate_capped_at_target(self, media_file, work_dirs, settings):
        settings.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_bitrate=5000)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[4] == 2250

    def test_video_bitrate_never_upscaled(self, media_file, work_dirs, settings):
        settings.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_bitrate=800)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[4] == 800

    @pytest.mark.parametrize("video_bitrate,expected", [(1300, 1950), (5000, 2250), (0, 2250)])
    def test_video_bitrate_capped_as_h264_equivalent(self, media_file, work_dirs, settings, video_bitrate, expected):
        settings.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_codec="hevc", video_bitrate=video_bitrate)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[4] == expected

    @pytest.mark.parametrize("video_codec,video_bitrate,expected", [("h264", 3000, 2000), ("hevc", 1500, 1500)])
    def test_video_bitrate_capped_in_target_codec_terms(
        self, media_file, work_dirs, settings, video_codec, video_bitrate, expected
    ):
        settings.target_video_codec = "hevc"
        settings.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_codec=video_codec, video_bitrate=video_bitrate)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[3] == "hevc"
        assert mock_cmd.call_args.args[4] == expected

    def test_passes_the_selected_audio_tracks(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[6] == encoder._processing_file.audio_tracks

    def test_unknown_source_video_bitrate_falls_back_to_target(self, media_file, work_dirs, settings):
        settings.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_bitrate=0)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[4] == 2250

    def test_copies_video_when_only_audio_needs_encoding(self, media_file, work_dirs):
        encoder = self._encoder(media_file, encode_video=False)
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()) as mock_popen,
            patch("baycone.encoding.ffmpeg.get_cmd_encode_audio", return_value=["ffmpeg"]) as mock_cmd,
        ):
            assert encoder._encode() is True
        assert mock_popen.call_count == 1
        assert mock_cmd.call_args.args[2] == encoder._processing_file.audio_tracks

    def test_kills_ffmpeg_when_interrupted(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        process = make_popen_mock()
        process.stdout = MagicMock(__iter__=MagicMock(side_effect=KeyboardInterrupt))
        with patch("baycone.encoding.subprocess.Popen", return_value=process):
            with pytest.raises(KeyboardInterrupt):
                encoder._encode()
        process.kill.assert_called_once()

    def test_logs_progress_without_dividing_by_zero_duration(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        encoder._processing_file.duration = 0
        progress = make_popen_mock(lines=["out_time_us=5000000\n", "progress=continue\n"])
        with patch("baycone.encoding.subprocess.Popen", side_effect=[progress, make_popen_mock()]):
            assert encoder._encode() is True


class TestEncoderOptimize:
    def _encoder(self, media_file: Path, audio_tracks: list[AudioTrack] | None = None) -> Encoder:
        encoder = encoder_for(
            FileInfo(
                media_file,
                is_valid=True,
                duration=100,
                audio_tracks=(
                    [AudioTrack(AudioStream(codec="aac", channels=2))] if audio_tracks is None else audio_tracks
                ),
            )
        )
        return encoder

    def test_returns_true_on_success(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()):
            assert encoder._optimize(encoded=False) is True

    def test_optimizes_video_without_audio_tracks(self, media_file, work_dirs):
        encoder = self._encoder(media_file, audio_tracks=[])
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()) as mock_popen:
            assert encoder._optimize(encoded=False) is True
        mock_popen.assert_called_once()

    def test_returns_false_on_ffmpeg_failure(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock(returncode=1)):
            assert encoder._optimize(encoded=False) is False

    def test_logs_ffmpeg_errors_on_failure(self, media_file, work_dirs, caplog):
        encoder = self._encoder(media_file)

        def fail(cmd, stderr, **kwargs):
            stderr.write("moov atom not found\n")
            return make_popen_mock(returncode=1)

        with patch("baycone.encoding.subprocess.Popen", side_effect=fail):
            encoder._optimize(encoded=False)
        assert "moov atom not found" in caplog.text

    def test_writes_to_the_work_directory_not_the_library(self, media_file, work_dirs):
        """A crash mid-write must not leave a broken file where the media server looks."""
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()),
            patch("baycone.encoding.ffmpeg.get_cmd_optimize", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._optimize(encoded=False)
        assert mock_cmd.call_args.args[1] == work_dirs.optimized_dir / "movie.mp4"

    def test_reads_the_original_when_not_encoded(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()),
            patch("baycone.encoding.ffmpeg.get_cmd_optimize", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._optimize(encoded=False)
        assert mock_cmd.call_args.args[0] == media_file

    def test_reads_the_encoded_file_when_encoded(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()),
            patch("baycone.encoding.ffmpeg.get_cmd_optimize", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._optimize(encoded=True)
        assert mock_cmd.call_args.args[0] == encoder._job.encoding_path
        assert mock_cmd.call_args.args[2:] == (encoder._processing_file.audio_tracks, True)


class TestLogging:
    def _encoder(self, media_file: Path, **kwargs) -> Encoder:
        encoder = encoder_for(
            FileInfo(
                media_file,
                is_valid=True,
                duration=kwargs.get("duration", 100),
                video_stream=VideoStream(
                    codec="hevc",
                    bitrate=5000,
                    shall_encode=True,
                    width=3840,
                    height=2160,
                    color_transfer="smpte2084",
                    crop_height=1600,
                    crop_top=280,
                ),
                audio_streams=[AudioStream(index=0), AudioStream(index=1), AudioStream(index=2)],
                audio_tracks=[
                    AudioTrack(AudioStream(codec="ac3", channels=6, lang="eng"), bitrate=384),
                    AudioTrack(AudioStream(codec="aac", channels=2, lang="ces")),
                ],
            )
        )
        return encoder

    def test_plan_describes_video_and_audio(self, media_file, work_dirs, caplog, settings):
        settings.target_video_bitrate = 2250
        caplog.set_level("INFO")
        self._encoder(media_file)._log_plan()
        assert (
            "Video: hevc 3840x2160 HDR 5000 kbps, encode to h264 2250 kbps"
            " (crop to 3840x1600, scale to fit 1920x1080, tone map to SDR)"
        ) in caplog.text
        assert (
            "Audio: eng Surround 5.1 (ac3 -> aac 384 kbps), ces Stereo (aac, copy); 1 track(s) dropped" in caplog.text
        )

    def test_progress_names_the_file_and_estimates_the_rest(self, media_file, work_dirs, caplog):
        caplog.set_level("DEBUG")
        encoder = self._encoder(media_file)
        progress = make_popen_mock(lines=["out_time_us=25000000\n"])
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=progress),
            patch.object(encoder, "_kill_when_stalled"),
            patch("baycone.encoding.time.monotonic", side_effect=[0, 0, 60, 60]),
        ):
            encoder._run_ffmpeg(["ffmpeg"], "Encoding 1st pass")
        assert "'movie.mp4' Encoding 1st pass: 0 %" in caplog.text
        assert "'movie.mp4' Encoding 1st pass: 20 %, ETA 0:03:00" in caplog.text  # 25 % done in 60 s
        assert "Encoding 1st pass finished in 0:01:00" in caplog.text


class TestCleanupWorkdirs:
    def test_clears_encoded_and_optimized_files(self, media_file, work_dirs):
        encoded = work_dirs.encoded_dir / "movie.mp4"
        optimized = work_dirs.optimized_dir / "movie.mp4"
        encoded.touch()
        optimized.touch()

        encoder = encoder_for(FileInfo(media_file))
        encoder._cleanup_workdirs()

        assert not encoded.exists()
        assert not optimized.exists()

    def test_keeps_the_first_pass_for_a_resume(self, media_file, work_dirs):
        passlog = work_dirs.passlog_dir / "passlog-0.log"
        passlog.touch()

        encoder = encoder_for(FileInfo(media_file))
        encoder._cleanup_workdirs()

        assert passlog.is_file()

    def test_failure_does_not_raise(self, media_file, work_dirs, caplog):
        """It runs in a `finally`: raising would turn a finished file into a failed one."""
        encoder = encoder_for(FileInfo(media_file))
        with patch("baycone.encoding.Path.mkdir", side_effect=OSError("read-only file system")):
            encoder._cleanup_workdirs()
        assert "read-only file system" in caplog.text

    def test_workdirs_still_exist_afterwards(self, media_file, work_dirs):
        encoder = encoder_for(FileInfo(media_file))
        encoder._cleanup_workdirs()

        assert work_dirs.encoded_dir.is_dir()
        assert work_dirs.optimized_dir.is_dir()

    def test_survives_missing_workdirs(self, media_file, work_dirs):
        import shutil as _shutil

        _shutil.rmtree(work_dirs.work_dir)

        encoder = encoder_for(FileInfo(media_file))
        encoder._cleanup_workdirs()

        assert work_dirs.encoded_dir.is_dir()

    def test_leaves_processed_output_untouched(self, media_file, work_dirs):
        processed = work_dirs.output_file.parent / "movie.mp4"
        processed.touch()

        encoder = encoder_for(FileInfo(media_file))
        encoder._cleanup_workdirs()

        assert processed.is_file()


class TestPublish:
    def _encoder(self, media_file: Path) -> Encoder:
        encoder = encoder_for(FileInfo(media_file, is_valid=True))
        encoder._job.optimized_path.write_text("finished")
        return encoder

    def test_moves_the_finished_file_into_the_library(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        encoder._publish()
        assert (work_dirs.output_file.parent / "movie.mp4").read_text() == "finished"
        assert not (work_dirs.optimized_dir / "movie.mp4").exists()

    def test_replaces_an_earlier_output(self, media_file, work_dirs):
        (work_dirs.output_file.parent / "movie.mp4").write_text("earlier")
        self._encoder(media_file)._publish()
        assert (work_dirs.output_file.parent / "movie.mp4").read_text() == "finished"

    def test_creates_the_output_directory(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        encoder._job.output_file = work_dirs.output_file.parent / "Show" / "episode.mp4"
        encoder._job.optimized_path.write_text("finished")

        encoder._publish()

        assert (work_dirs.output_file.parent / "Show" / "episode.mp4").read_text() == "finished"

    def test_copies_across_filesystems(self, media_file, work_dirs):
        """A rename cannot cross filesystems; the output still appears complete in one step."""
        encoder = self._encoder(media_file)
        real_replace = Path.replace

        def replace(path, target):
            if path == work_dirs.optimized_path:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(path, target)

        with (
            patch("baycone.encoding.Path.replace", replace),
            patch("baycone.encoding.fsync"),
            patch("baycone.encoding.fsync_dir"),
        ):
            encoder._publish()

        assert work_dirs.output_file.read_text() == "finished"
        assert not work_dirs.optimized_path.exists()
        assert list(work_dirs.output_file.parent.iterdir()) == [work_dirs.output_file]

    def test_syncs_the_file_before_the_rename_and_the_directory_after(self, media_file, work_dirs):
        """After a power loss the output must not be empty while the original is already archived."""
        encoder = self._encoder(media_file)
        synced = []
        with (
            patch("baycone.encoding.fsync", side_effect=lambda path: synced.append((path, path.exists()))),
            patch("baycone.encoding.fsync_dir", side_effect=lambda path: synced.append((path, True))),
        ):
            encoder._publish()
        assert synced == [(work_dirs.optimized_dir / "movie.mp4", True), (work_dirs.output_file.parent, True)]

    def test_failed_sync_leaves_the_library_untouched(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.fsync", side_effect=OSError("I/O error")):
            with pytest.raises(OSError):
                encoder._publish()
        assert not (work_dirs.output_file.parent / "movie.mp4").exists()


class TestFreeSpace:
    def _encoder(self, media_file: Path, size: int = 1000, **kwargs) -> Encoder:
        media_file.write_bytes(b"x" * size)
        encoder = Encoder()
        encoder._signature = file_signature(media_file)
        encoder._job = job_for(media_file)
        encoder._processing_file = FileInfo(
            media_file,
            is_valid=True,
            duration=kwargs.get("duration", 100),
            video_stream=VideoStream(codec="h264", bitrate=5000, shall_encode=kwargs.get("encode_video", False)),
            audio_tracks=[
                AudioTrack(AudioStream(codec="ac3", channels=6), bitrate=384),
                AudioTrack(AudioStream(codec="aac", channels=2, channel_bitrate=64)),
            ],
        )
        return encoder

    def _check(self, encoder: Encoder, free: int, shall_encode: bool = False, total: int = 10**12) -> None:
        with patch("baycone.encoding.shutil.disk_usage", return_value=DiskUsage(total, total - free, free)):
            encoder._check_free_space(shall_encode)

    def test_remux_needs_the_source_size(self, media_file, work_dirs):
        assert self._encoder(media_file)._required_space(shall_encode=False) == 1000

    def test_audio_only_encode_needs_the_source_size_twice(self, media_file, work_dirs):
        assert self._encoder(media_file)._required_space(shall_encode=True) == 2000

    def test_video_encode_needs_the_target_bitrate_twice(self, media_file, work_dirs, settings):
        settings.target_video_bitrate = 2250
        encoder = self._encoder(media_file, encode_video=True)
        # (2250 video + 384 encoded audio + 2 x 64 copied audio) kbps for 100 s, encoded and optimized
        assert encoder._required_space(shall_encode=True) == (2250 + 384 + 128) * 125 * 100 * 2

    def test_unknown_duration_falls_back_to_the_source_size(self, media_file, work_dirs):
        encoder = self._encoder(media_file, encode_video=True, duration=0)
        assert encoder._required_space(shall_encode=True) == 2000

    def test_enough_space_passes(self, media_file, work_dirs):
        self._check(self._encoder(media_file), free=1000)

    def test_too_little_space_raises(self, media_file, work_dirs):
        with pytest.raises(InsufficientSpaceError, match="movie.mp4"):
            self._check(self._encoder(media_file), free=999)

    def test_filesystem_without_space_report_is_not_checked(self, media_file, work_dirs):
        """Some network filesystems report an empty disk rather than their real numbers."""
        self._check(self._encoder(media_file), free=0, total=0)

    def test_failing_space_report_is_not_checked(self, media_file, work_dirs):
        with patch("baycone.encoding.shutil.disk_usage", side_effect=OSError("not supported")):
            self._encoder(media_file)._check_free_space(False)

    def test_check_can_be_turned_off_per_location(self, media_file, work_dirs, settings):
        settings.check_free_space = False
        self._check(self._encoder(media_file), free=0)


class TestStallWatchdog:
    @pytest.fixture(autouse=True)
    def short_timeouts(self, monkeypatch):
        monkeypatch.setattr("baycone.encoding.STALL_CHECK_SECONDS", 0.01)
        monkeypatch.setattr("baycone.encoding.STALL_SECONDS", 0.05)

    def _encoder(self, media_file: Path) -> Encoder:
        encoder = encoder_for(FileInfo(media_file, is_valid=True, duration=100))
        return encoder

    def _hanging_process(self) -> MagicMock:
        """Its output only ends once it is killed."""
        killed = threading.Event()

        def stdout():
            killed.wait(5)
            return
            yield

        process = MagicMock(returncode=-9)
        process.stdout = stdout()
        process.kill.side_effect = killed.set
        return process

    def test_kills_ffmpeg_that_makes_no_progress(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        process = self._hanging_process()
        with patch("baycone.encoding.subprocess.Popen", return_value=process):
            with pytest.raises(subprocess.CalledProcessError) as error:
                encoder._run_ffmpeg(["ffmpeg"], "Encoding 1st pass")
        process.kill.assert_called_once()
        assert "No progress" in error.value.stderr

    def test_stalled_encode_fails_the_file(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=self._hanging_process()):
            assert encoder._optimize(encoded=False) is False

    def _watch(self, encoder: Encoder, output: Path | None, between_checks) -> MagicMock:
        """Six checks 20 s apart on a fake clock, against a stall limit of 50 s; no real time passes."""
        process, done = MagicMock(), MagicMock()

        def wait(timeout):
            between_checks()
            return done.wait.call_count > 6

        done.wait.side_effect = wait
        with (
            patch("baycone.encoding.STALL_SECONDS", 50.0),
            patch("baycone.encoding.time.monotonic", side_effect=itertools.count(0, 20)),
        ):
            encoder._kill_when_stalled(process, output, done)
        return process

    def test_no_progress_is_a_stall(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        process = self._watch(encoder, None, lambda: None)
        process.kill.assert_called_once()
        assert encoder._stalled is True

    def test_advancing_progress_keeps_ffmpeg_alive(self, media_file, work_dirs):
        encoder = self._encoder(media_file)

        def advance():
            encoder._out_time += 1

        self._watch(encoder, None, advance).kill.assert_not_called()

    def test_growing_output_keeps_ffmpeg_alive(self, media_file, work_dirs):
        """Moving the index to the front rewrites the file without reporting any progress."""
        output = work_dirs.optimized_dir / "movie.mp4"

        def grow():
            with output.open("ab") as file:
                file.write(b"x")

        self._watch(self._encoder(media_file), output, grow).kill.assert_not_called()

    def test_output_that_stopped_growing_is_a_stall(self, media_file, work_dirs):
        output = work_dirs.optimized_dir / "movie.mp4"
        output.write_bytes(b"x")
        self._watch(self._encoder(media_file), output, lambda: None).kill.assert_called_once()

    def test_finished_ffmpeg_is_not_reported_as_stalled(self, media_file, work_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()):
            encoder._run_ffmpeg(["ffmpeg"], "Optimizing")
        assert encoder._stalled is False


class TestFirstPassResume:
    def _encoder(self, media_file: Path) -> Encoder:
        encoder = Encoder()
        encoder._signature = file_signature(media_file)
        encoder._job = job_for(media_file)
        encoder._processing_file = FileInfo(
            media_file,
            is_valid=True,
            duration=100,
            video_stream=VideoStream(codec="h264", bitrate=5000, shall_encode=True),
            audio_tracks=[AudioTrack(AudioStream(codec="aac", channels=2), bitrate=128)],
        )
        return encoder

    def _encode(self, encoder: Encoder, *processes: MagicMock) -> tuple[bool, list[str]]:
        """The result and which passes ran."""
        with patch("baycone.encoding.subprocess.Popen", side_effect=list(processes)) as mock_popen:
            result = encoder._encode()
        return result, [
            "2nd" if "2" in call.args[0][call.args[0].index("-pass") + 1] else "1st"
            for call in mock_popen.call_args_list
        ]

    def _interrupted_second_pass(self, media_file: Path) -> None:
        process = make_popen_mock()
        process.stdout = MagicMock(__iter__=MagicMock(side_effect=KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            self._encode(self._encoder(media_file), make_popen_mock(), process)

    def test_first_pass_is_marked_done(self, media_file, work_dirs):
        self._interrupted_second_pass(media_file)
        assert (work_dirs.passlog_dir / "pass1.json").is_file()

    def test_resumes_with_the_second_pass(self, media_file, work_dirs):
        self._interrupted_second_pass(media_file)
        assert self._encode(self._encoder(media_file), make_popen_mock()) == (True, ["2nd"])

    def test_first_pass_that_did_not_finish_is_repeated(self, media_file, work_dirs):
        self._encode(self._encoder(media_file), make_popen_mock(returncode=1))
        assert self._encode(self._encoder(media_file), make_popen_mock(), make_popen_mock()) == (True, ["1st", "2nd"])

    def test_changed_source_repeats_the_first_pass(self, media_file, work_dirs):
        self._interrupted_second_pass(media_file)
        media_file.write_text("another movie")
        assert self._encode(self._encoder(media_file), make_popen_mock(), make_popen_mock()) == (True, ["1st", "2nd"])

    def test_changed_settings_repeat_the_first_pass(self, media_file, work_dirs, settings):
        self._interrupted_second_pass(media_file)
        settings.target_video_bitrate = 1500
        assert self._encode(self._encoder(media_file), make_popen_mock(), make_popen_mock()) == (True, ["1st", "2nd"])

    def test_stale_first_pass_of_another_file_is_cleared(self, media_file, work_dirs):
        self._interrupted_second_pass(media_file)
        stats = work_dirs.passlog_dir / "passlog-0.log"
        stats.touch()
        media_file.write_text("another movie")
        self._encode(self._encoder(media_file), make_popen_mock(), make_popen_mock())
        assert not stats.exists()

    def test_starts_over_when_the_kept_first_pass_does_not_fit(self, media_file, work_dirs):
        """E.g. after an FFmpeg upgrade; without the fallback the file would fail for good."""
        self._interrupted_second_pass(media_file)
        result = self._encode(
            self._encoder(media_file), make_popen_mock(returncode=1), make_popen_mock(), make_popen_mock()
        )
        assert result == (True, ["2nd", "1st", "2nd"])

    def test_fresh_second_pass_failure_is_not_repeated(self, media_file, work_dirs):
        result = self._encode(self._encoder(media_file), make_popen_mock(), make_popen_mock(returncode=1))
        assert result == (False, ["1st", "2nd"])

    def test_passlog_is_cleared_once_the_encode_is_done(self, media_file, work_dirs):
        self._encode(self._encoder(media_file), make_popen_mock(), make_popen_mock())
        assert list(work_dirs.passlog_dir.iterdir()) == []

    def test_stats_are_synced_before_the_marker_is_written(self, media_file, work_dirs):
        synced = []

        def ffmpeg_writing_stats(cmd, **kwargs):
            (work_dirs.passlog_dir / "passlog-0.log").touch()
            return make_popen_mock()

        def record(path):
            synced.append((path.name, (work_dirs.passlog_dir / "pass1.json").exists()))

        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=ffmpeg_writing_stats),
            patch("baycone.encoding.fsync", side_effect=record),
        ):
            self._encoder(media_file)._encode()
        assert synced == [("passlog-0.log", False)]
