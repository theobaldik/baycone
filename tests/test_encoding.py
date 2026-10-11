import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone import ffmpeg
from baycone.audio import AudioStream, AudioTrack
from baycone.config import SpecialDirs
from baycone.encoding import Analyzer, Encoder, FileInfo, VideoStream


def make_ffprobe_output(
    container: str = "mp4",
    duration: float = 100.0,
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
            ],
        }
    )


def make_popen_mock(returncode: int = 0, lines: list[str] | None = None) -> MagicMock:
    process = MagicMock()
    process.stdout = iter(lines or [])
    process.returncode = returncode
    return process


@pytest.fixture
def special_dirs(location_config) -> SpecialDirs:
    dirs = SpecialDirs(location_config.abspath)
    dirs.create()
    return dirs


@pytest.fixture
def media_file(location_dir) -> Path:
    video = location_dir / "movie.mp4"
    video.touch()
    return video


def analyze(file_path: Path, ffprobe_output: str, method=Analyzer.analyze) -> FileInfo:
    with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout=ffprobe_output)):
        return method(file_path)


class TestFileInfoPaths:
    def test_resolves_location_config(self, media_file, location_config):
        assert FileInfo(media_file).location_config.path == location_config.path

    def test_resolves_special_dirs_root(self, media_file, location_dir):
        assert FileInfo(media_file).special_dirs.root_dir == location_dir

    def test_relative_path(self, media_file):
        assert FileInfo(media_file).relative_path == Path("movie.mp4")

    def test_relative_path_in_subdirectory(self, location_dir):
        video = location_dir / "ShowName" / "episode.mkv"
        video.parent.mkdir(parents=True)
        video.touch()
        assert FileInfo(video).relative_path == Path("ShowName/episode.mkv")

    def test_passlog_path(self, media_file, special_dirs):
        assert FileInfo(media_file).passlog_path == special_dirs.passlog_dir / "movie.mp4"

    def test_encoding_path_uses_target_extension(self, location_dir, special_dirs):
        video = location_dir / "movie.mkv"
        video.touch()
        assert FileInfo(video).encoding_path == special_dirs.encoded_dir / "movie.mp4"

    def test_original_path_keeps_source_extension(self, location_dir, special_dirs):
        video = location_dir / "movie.mkv"
        video.touch()
        assert FileInfo(video).original_path == special_dirs.originals_dir / "movie.mkv"

    def test_processed_path_uses_target_extension(self, location_dir, special_dirs):
        video = location_dir / "movie.mkv"
        video.touch()
        assert FileInfo(video).processed_path == special_dirs.processed_dir / "movie.mp4"

    def test_rejects_none(self, location_config):
        with pytest.raises(ValueError):
            FileInfo(None)

    def test_rejects_non_path(self, location_config):
        with pytest.raises(TypeError):
            FileInfo("/movies/movie.mp4")

    def test_rejects_path_outside_any_location(self, location_config, tmp_path):
        with pytest.raises(ValueError):
            FileInfo(tmp_path.parent / "elsewhere" / "movie.mp4")


class TestGetFfprobeResult:
    def test_returns_parsed_dict_on_success(self, media_file):
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout=make_ffprobe_output())):
            result = Analyzer._get_ffprobe_result(media_file)
        assert isinstance(result, dict)
        assert "streams" in result

    def test_returns_none_for_missing_file(self, location_dir):
        assert Analyzer._get_ffprobe_result(location_dir / "nonexistent.mp4") is None

    def test_returns_none_on_subprocess_error(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            assert Analyzer._get_ffprobe_result(media_file) is None

    def test_returns_none_on_timeout(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            assert Analyzer._get_ffprobe_result(media_file) is None

    def test_returns_none_on_invalid_json(self, media_file):
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stdout="not json")):
            assert Analyzer._get_ffprobe_result(media_file) is None


class TestGetFileInfo:
    def _info(self, media_file: Path, **kwargs) -> FileInfo:
        return analyze(media_file, make_ffprobe_output(**kwargs), method=Analyzer._get_file_info)

    def test_is_valid_on_successful_probe(self, media_file):
        assert self._info(media_file).is_valid is True

    def test_is_invalid_when_ffprobe_fails(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            assert Analyzer._get_file_info(media_file).is_valid is False

    def test_duration_defaults_to_zero_when_invalid(self, media_file):
        with patch("baycone.encoding.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            assert Analyzer._get_file_info(media_file).duration == 0

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
        info = analyze(media_file, json.dumps(output), method=Analyzer._get_file_info)
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
        info = analyze(media_file, payload, method=Analyzer._get_file_info)
        assert [s.index for s in info.audio_streams] == [0, 1]

    def test_original_disposition_extracted(self, media_file):
        assert self._info(media_file, audio_disposition={"original": 1}).audio_streams[0].original is True

    @pytest.mark.parametrize("disposition", ["comment", "visual_impaired", "descriptions", "hearing_impaired"])
    def test_special_disposition_extracted(self, media_file, disposition):
        assert self._info(media_file, audio_disposition={disposition: 1}).audio_streams[0].special is True

    def test_dub_is_not_special(self, media_file):
        assert self._info(media_file, audio_disposition={"dub": 1, "default": 1}).audio_streams[0].special is False

    def test_video_geometry_and_colour_extracted(self, media_file):
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
        info = analyze(media_file, payload, method=Analyzer._get_file_info)
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
        assert analyze(media_file, payload, method=Analyzer._get_file_info).video_stream.bitrate == 3000

    def test_no_audio_streams_on_video_only_file(self, media_file):
        payload = json.dumps(
            {
                "format": {"format_name": "mp4", "duration": "100.0"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "bit_rate": "1000000"}],
            }
        )
        assert analyze(media_file, payload, method=Analyzer._get_file_info).audio_streams == []


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

    def test_encode_needed_video_bitrate_over_limit(self, media_file, location_config):
        location_config.limit_video_bitrate = 1000
        assert self._analyze(media_file, video_bitrate=2_000_000).video_stream.shall_encode is True

    def test_encode_needed_wrong_audio_codec(self, media_file):
        assert self._analyze(media_file, audio_codec="ac3").audio_streams[0].shall_encode is True

    def test_audio_codec_check_is_not_a_substring_match(self, media_file, location_config):
        """Regression: the check used to be `codec not in target_audio_codec` (a substring test on a string)."""
        location_config.allowed_audio_codecs = ["aac"]
        assert self._analyze(media_file, audio_codec="aa").audio_streams[0].shall_encode is True

    def test_audio_codec_check_uses_allowed_audio_codecs(self, media_file, location_config):
        location_config.allowed_audio_codecs = ["aac", "ac3"]
        assert self._analyze(media_file, audio_codec="ac3").audio_streams[0].shall_encode is False

    def test_encode_needed_audio_bitrate_over_limit(self, media_file, location_config):
        location_config.limit_audio_channel_bitrate = 64
        info = self._analyze(media_file, audio_bitrate=256_000, audio_channels=2)
        assert info.audio_streams[0].shall_encode is True

    def test_selects_audio_tracks(self, media_file):
        info = self._analyze(media_file)
        assert [t.stream for t in info.audio_tracks] == info.audio_streams

    def test_selected_track_needing_encode_gets_a_bitrate(self, media_file, location_config):
        location_config.target_audio_channel_bitrate = 64
        info = self._analyze(media_file, audio_codec="ac3", audio_bitrate=448_000, audio_channels=6)
        assert info.audio_tracks[0].bitrate == 384


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
        with (
            patch.object(Analyzer, "analyze", return_value=file_info),
            patch.object(encoder, "_encode", **mocks.get("encode", {"return_value": True})) as mock_encode,
            patch.object(encoder, "_optimize", **mocks.get("optimize", {"return_value": True})) as mock_optimize,
            patch.object(encoder, "_archive_original") as mock_archive,
            patch.object(encoder, "_cleanup_workdirs") as mock_cleanup,
            patch.object(Analyzer, "detect_crop", **mocks.get("detect_crop", {})),
        ):
            result = encoder.process(media_file)
        return result, mock_encode, mock_optimize, mock_archive, mock_cleanup

    def test_detects_black_bars_only_when_the_video_is_encoded(self, media_file, special_dirs):
        detect = MagicMock()
        self._process(Encoder(), self._file_info(media_file), media_file, detect_crop={"new": detect})
        detect.assert_not_called()

    def test_detects_black_bars_when_the_video_is_encoded(self, media_file, special_dirs):
        detect = MagicMock()
        info = self._file_info(media_file, video_shall_encode=True)
        self._process(Encoder(), info, media_file, detect_crop={"new": detect})
        detect.assert_called_once_with(media_file, info.video_stream)

    def test_logs_a_headline_before_anything_else(self, media_file, special_dirs, caplog):
        caplog.set_level("INFO")
        self._process(Encoder(), self._file_info(media_file), media_file)
        assert caplog.messages[0] == "Processing 'movie.mp4' (0:01:40)"

    def test_invalid_file_is_not_described(self, media_file, special_dirs, caplog):
        caplog.set_level("INFO")
        info = FileInfo(media_file, is_valid=False)
        self._process(Encoder(), info, media_file, encode={"return_value": False})
        assert not [m for m in caplog.messages if m.startswith(("Processing '", "Video:", "Audio:"))]

    def test_invalid_file_is_never_archived(self, media_file, special_dirs):
        info = FileInfo(media_file, is_valid=False)
        result, encode, optimize, archive, cleanup = self._process(Encoder(), info, media_file)
        assert result is False
        encode.assert_not_called()
        optimize.assert_not_called()
        archive.assert_not_called()
        cleanup.assert_called_once()

    def test_skips_encode_when_not_needed(self, media_file, special_dirs):
        encoder = Encoder()
        result, encode, optimize, archive, _ = self._process(encoder, self._file_info(media_file), media_file)
        assert result is True
        encode.assert_not_called()
        optimize.assert_called_once_with(False)
        archive.assert_called_once()

    def test_wrong_container_is_only_remuxed(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, shall_remux=True)
        _, encode, optimize, _, _ = self._process(encoder, info, media_file)
        encode.assert_not_called()
        optimize.assert_called_once_with(False)

    def test_calls_encode_when_needed(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        _, encode, optimize, archive, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()
        optimize.assert_called_once_with(True)
        archive.assert_called_once()

    def test_encode_triggered_by_video_stream(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        _, encode, _, _, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()

    def test_encode_triggered_by_audio_stream(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, audio_shall_encode=True)
        _, encode, _, _, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()

    def test_dropped_audio_streams_never_trigger_encode(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file)
        info.audio_streams = [AudioStream(codec="dts", shall_encode=True)]
        _, encode, optimize, _, _ = self._process(encoder, info, media_file)
        encode.assert_not_called()
        optimize.assert_called_once_with(False)

    def test_aborts_without_archiving_when_encode_fails(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        result, _, optimize, archive, cleanup = self._process(encoder, info, media_file, encode={"return_value": False})
        assert result is False
        optimize.assert_not_called()
        archive.assert_not_called()
        cleanup.assert_called_once()

    def test_aborts_without_archiving_when_optimize_fails(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        result, _, _, archive, cleanup = self._process(encoder, info, media_file, optimize={"return_value": False})
        assert result is False
        archive.assert_not_called()
        cleanup.assert_called_once()

    def test_cleans_up_workdirs_even_when_a_step_raises(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        with (
            patch.object(Analyzer, "analyze", return_value=info),
            patch.object(Analyzer, "detect_crop"),
            patch.object(encoder, "_encode", side_effect=RuntimeError("boom")),
            patch.object(encoder, "_cleanup_workdirs") as mock_cleanup,
        ):
            with pytest.raises(RuntimeError):
                encoder.process(media_file)
        mock_cleanup.assert_called_once()

    def test_resets_processing_file_after_completion(self, media_file, special_dirs):
        encoder = Encoder()
        self._process(encoder, self._file_info(media_file), media_file)
        assert encoder._processing_file is None


class TestVideoEncodeDecision:
    def _video(self, media_file: Path, **kwargs) -> VideoStream:
        return analyze(media_file, make_ffprobe_output(**kwargs)).video_stream

    def test_copied_when_within_limits(self, media_file):
        assert self._video(media_file).shall_encode is False

    def test_encoded_when_bitrate_is_unknown(self, media_file):
        assert self._video(media_file, video_bitrate=0).shall_encode is True

    def test_encoded_when_10_bit(self, media_file):
        assert self._video(media_file, video_pix_fmt="yuv420p10le").shall_encode is True

    def test_encoded_when_taller_than_the_limit(self, media_file):
        assert self._video(media_file, video_width=3840, video_height=2160).shall_encode is True

    def test_encoded_when_wider_than_the_box(self, media_file):
        assert self._video(media_file, video_width=2560, video_height=1080).shall_encode is True

    def test_limit_follows_the_config(self, media_file, location_config):
        location_config.limit_video_height = 720
        assert self._video(media_file).shall_encode is True

    def test_audio_encoded_when_bitrate_is_unknown(self, media_file):
        info = analyze(media_file, make_ffprobe_output(audio_bitrate=0))
        assert info.audio_streams[0].shall_encode is True


class TestDetectCrop:
    STDERR = "[Parsed_cropdetect_0] x1:0 x2:1919 y1:140 y2:939 w:1920 h:800 x:0 y:140 crop=1920:800:0:140\n"

    def _video(self, media_file: Path, stderr: str = STDERR, **kwargs) -> VideoStream:
        video = VideoStream(width=1920, height=1080)
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(stderr=stderr), **kwargs):
            Analyzer.detect_crop(media_file, video)
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

    def test_failure_is_no_crop(self, media_file):
        video = self._video(media_file, side_effect=subprocess.CalledProcessError(1, "ffmpeg", stderr="boom"))
        assert video.crop_height == 0


class TestVideoFilters:
    def _filters(self, media_file: Path, **kwargs) -> list[str]:
        encoder = Encoder()
        encoder._processing_file = FileInfo(media_file, video_stream=VideoStream(**kwargs))
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
        encoder = Encoder()
        encoder._processing_file = FileInfo(
            media_file,
            is_valid=kwargs.get("is_valid", True),
            duration=100,
            video_stream=VideoStream(
                codec="h264", bitrate=kwargs.get("video_bitrate", 5000), shall_encode=kwargs.get("encode_video", True)
            ),
            audio_tracks=kwargs.get("audio_tracks", [AudioTrack(AudioStream(codec="aac", channels=2), bitrate=128)]),
        )
        return encoder

    def test_returns_true_on_success(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]):
            assert encoder._encode() is True

    def test_runs_two_ffmpeg_passes(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with patch(
            "baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]
        ) as mock_popen:
            encoder._encode()
        assert mock_popen.call_count == 2

    def test_returns_false_for_invalid_file(self, media_file, special_dirs):
        encoder = self._encoder(media_file, is_valid=False)
        with patch("baycone.encoding.subprocess.Popen") as mock_popen:
            assert encoder._encode() is False
        mock_popen.assert_not_called()

    def test_encodes_video_without_audio_tracks(self, media_file, special_dirs):
        encoder = self._encoder(media_file, audio_tracks=[])
        with patch(
            "baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]
        ) as mock_popen:
            assert encoder._encode() is True
        assert mock_popen.call_count == 2

    def test_returns_false_when_first_pass_fails(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock(returncode=1)):
            assert encoder._encode() is False

    def test_returns_false_when_second_pass_fails(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        passes = [make_popen_mock(), make_popen_mock(returncode=1)]
        with patch("baycone.encoding.subprocess.Popen", side_effect=passes):
            assert encoder._encode() is False

    def test_video_bitrate_capped_at_target(self, media_file, special_dirs, location_config):
        location_config.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_bitrate=5000)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[3] == 2250

    def test_video_bitrate_never_upscaled(self, media_file, special_dirs, location_config):
        location_config.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_bitrate=800)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[3] == 800

    def test_passes_the_selected_audio_tracks(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[5] == encoder._processing_file.audio_tracks

    def test_unknown_source_video_bitrate_falls_back_to_target(self, media_file, special_dirs, location_config):
        location_config.target_video_bitrate = 2250
        encoder = self._encoder(media_file, video_bitrate=0)
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[3] == 2250

    def test_copies_video_when_only_audio_needs_encoding(self, media_file, special_dirs):
        encoder = self._encoder(media_file, encode_video=False)
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()) as mock_popen,
            patch("baycone.encoding.ffmpeg.get_cmd_encode_audio", return_value=["ffmpeg"]) as mock_cmd,
        ):
            assert encoder._encode() is True
        assert mock_popen.call_count == 1
        assert mock_cmd.call_args.args[2] == encoder._processing_file.audio_tracks

    def test_kills_ffmpeg_when_interrupted(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        process = make_popen_mock()
        process.stdout = MagicMock(__iter__=MagicMock(side_effect=KeyboardInterrupt))
        with patch("baycone.encoding.subprocess.Popen", return_value=process):
            with pytest.raises(KeyboardInterrupt):
                encoder._encode()
        process.kill.assert_called_once()

    def test_logs_progress_without_dividing_by_zero_duration(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        encoder._processing_file.duration = 0
        progress = make_popen_mock(lines=["out_time_us=5000000\n", "progress=continue\n"])
        with patch("baycone.encoding.subprocess.Popen", side_effect=[progress, make_popen_mock()]):
            assert encoder._encode() is True


class TestEncoderOptimize:
    def _encoder(self, media_file: Path, audio_tracks: list[AudioTrack] | None = None) -> Encoder:
        encoder = Encoder()
        encoder._processing_file = FileInfo(
            media_file,
            is_valid=True,
            duration=100,
            audio_tracks=[AudioTrack(AudioStream(codec="aac", channels=2))] if audio_tracks is None else audio_tracks,
        )
        return encoder

    def test_returns_true_on_success(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()):
            assert encoder._optimize(encoded=False) is True

    def test_optimizes_video_without_audio_tracks(self, media_file, special_dirs):
        encoder = self._encoder(media_file, audio_tracks=[])
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()) as mock_popen:
            assert encoder._optimize(encoded=False) is True
        mock_popen.assert_called_once()

    def test_returns_false_on_ffmpeg_failure(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock(returncode=1)):
            assert encoder._optimize(encoded=False) is False

    def test_logs_ffmpeg_errors_on_failure(self, media_file, special_dirs, caplog):
        encoder = self._encoder(media_file)

        def fail(cmd, stderr, **kwargs):
            stderr.write("moov atom not found\n")
            return make_popen_mock(returncode=1)

        with patch("baycone.encoding.subprocess.Popen", side_effect=fail):
            encoder._optimize(encoded=False)
        assert "moov atom not found" in caplog.text

    def test_reads_the_original_when_not_encoded(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()),
            patch("baycone.encoding.ffmpeg.get_cmd_optimize", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._optimize(encoded=False)
        assert mock_cmd.call_args.args[0] == media_file

    def test_reads_the_encoded_file_when_encoded(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=make_popen_mock()),
            patch("baycone.encoding.ffmpeg.get_cmd_optimize", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._optimize(encoded=True)
        assert mock_cmd.call_args.args[0] == encoder._processing_file.encoding_path
        assert mock_cmd.call_args.args[2:] == (encoder._processing_file.audio_tracks, True)


class TestLogging:
    def _encoder(self, media_file: Path, **kwargs) -> Encoder:
        encoder = Encoder()
        encoder._processing_file = FileInfo(
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
        return encoder

    def test_plan_describes_video_and_audio(self, media_file, special_dirs, caplog, location_config):
        location_config.target_video_bitrate = 2250
        caplog.set_level("INFO")
        self._encoder(media_file)._log_plan()
        assert (
            "Video: hevc 3840x2160 HDR 5000 kbps, encode to h264 2250 kbps"
            " (crop to 3840x1600, scale to fit 1920x1080, tone map to SDR)"
        ) in caplog.text
        assert (
            "Audio: eng Surround 5.1 (ac3 -> aac 384 kbps), ces Stereo (aac, copy); 1 track(s) dropped" in caplog.text
        )

    def test_progress_names_the_file_and_estimates_the_rest(self, media_file, special_dirs, caplog):
        caplog.set_level("INFO")
        encoder = self._encoder(media_file)
        progress = make_popen_mock(lines=["out_time_us=25000000\n"])
        with (
            patch("baycone.encoding.subprocess.Popen", return_value=progress),
            patch("baycone.encoding.time.monotonic", side_effect=[0, 0, 60, 60]),
        ):
            encoder._run_ffmpeg(["ffmpeg"], "Encoding 1st pass")
        assert "'movie.mp4' Encoding 1st pass: 0 %" in caplog.text
        assert "'movie.mp4' Encoding 1st pass: 20 %, ETA 0:03:00" in caplog.text  # 25 % done in 60 s
        assert "Encoding 1st pass finished in 0:01:00" in caplog.text


class TestArchiveOriginal:
    def _encoder(self, file_path: Path) -> Encoder:
        encoder = Encoder()
        encoder._processing_file = FileInfo(file_path)
        return encoder

    def test_moves_original_to_originals_dir(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        encoder._archive_original()
        assert not media_file.exists()
        assert (special_dirs.originals_dir / "movie.mp4").is_file()

    def test_keeps_subdirectory_structure(self, location_dir, special_dirs):
        video = location_dir / "ShowName" / "Season 1" / "episode.mkv"
        video.parent.mkdir(parents=True)
        video.touch()

        self._encoder(video)._archive_original()

        assert (special_dirs.originals_dir / "ShowName" / "Season 1" / "episode.mkv").is_file()

    def test_removes_directories_left_empty(self, location_dir, special_dirs):
        video = location_dir / "ShowName" / "Season 1" / "episode.mkv"
        video.parent.mkdir(parents=True)
        video.touch()

        self._encoder(video)._archive_original()

        assert not (location_dir / "ShowName").exists()

    def test_keeps_directories_holding_unprocessed_files(self, location_dir, special_dirs):
        """Regression: cleanup used to rmtree the whole top-level subdirectory, deleting pending files."""
        season_dir = location_dir / "ShowName" / "Season 1"
        season_dir.mkdir(parents=True)
        processed = season_dir / "episode01.mkv"
        pending = season_dir / "episode02.mkv"
        processed.touch()
        pending.touch()

        self._encoder(processed)._archive_original()

        assert pending.is_file()
        assert season_dir.is_dir()

    def test_keeps_sibling_directories(self, location_dir, special_dirs):
        show_dir = location_dir / "ShowName"
        season_1 = show_dir / "Season 1"
        season_2 = show_dir / "Season 2"
        season_1.mkdir(parents=True)
        season_2.mkdir(parents=True)
        processed = season_1 / "episode01.mkv"
        pending = season_2 / "episode01.mkv"
        processed.touch()
        pending.touch()

        self._encoder(processed)._archive_original()

        assert pending.is_file()
        assert not season_1.exists()
        assert show_dir.is_dir()

    def test_never_removes_the_location_root(self, media_file, location_dir, special_dirs):
        self._encoder(media_file)._archive_original()
        assert location_dir.is_dir()


class TestCleanupWorkdirs:
    def test_clears_passlog_and_encoded_files(self, media_file, special_dirs):
        passlog = special_dirs.passlog_dir / "movie.mp4-0.log"
        encoded = special_dirs.encoded_dir / "movie.mp4"
        passlog.touch()
        encoded.touch()

        encoder = Encoder()
        encoder._processing_file = FileInfo(media_file)
        encoder._cleanup_workdirs()

        assert not passlog.exists()
        assert not encoded.exists()

    def test_workdirs_still_exist_afterwards(self, media_file, special_dirs):
        encoder = Encoder()
        encoder._processing_file = FileInfo(media_file)
        encoder._cleanup_workdirs()

        assert special_dirs.passlog_dir.is_dir()
        assert special_dirs.encoded_dir.is_dir()

    def test_survives_missing_workdirs(self, media_file, special_dirs):
        import shutil as _shutil

        _shutil.rmtree(special_dirs.temp_dir)

        encoder = Encoder()
        encoder._processing_file = FileInfo(media_file)
        encoder._cleanup_workdirs()

        assert special_dirs.passlog_dir.is_dir()

    def test_leaves_processed_output_untouched(self, media_file, special_dirs):
        processed = special_dirs.processed_dir / "movie.mp4"
        processed.touch()

        encoder = Encoder()
        encoder._processing_file = FileInfo(media_file)
        encoder._cleanup_workdirs()

        assert processed.is_file()
