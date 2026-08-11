"""
Tests for baycone/encoding.py

Strategy:
- Analyzer tests inject a fake ffprobe payload by mocking `subprocess.run` - no real ffprobe needed.
- Encoder tests mock `subprocess.Popen` / `subprocess.run` - no real ffmpeg needed.
- `FileInfo` resolves its `LocationConfig` and `SpecialDirs` from the global config via the `file_path`
  setter, so every test relies on the `location_config` fixture (see conftest.py).
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone.config import SpecialDirs
from baycone.encoding import Analyzer, AudioStream, Encoder, FileInfo, VideoStream

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def make_ffprobe_output(
    container: str = "mp4",
    duration: float = 100.0,
    video_codec: str = "h264",
    video_bitrate: int = 2_000_000,
    audio_codec: str = "aac",
    audio_bitrate: int = 128_000,
    audio_channels: int = 2,
    audio_lang: str = "eng",
) -> str:
    return json.dumps(
        {
            "format": {"format_name": container, "duration": str(duration)},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": video_codec,
                    "bit_rate": str(video_bitrate),
                },
                {
                    "codec_type": "audio",
                    "codec_name": audio_codec,
                    "bit_rate": str(audio_bitrate),
                    "channels": audio_channels,
                    "tags": {"language": audio_lang},
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


# ---------------------------------------------------------------------------
# AudioStream
# ---------------------------------------------------------------------------


class TestAudioStream:
    @pytest.mark.parametrize(
        "channels,expected",
        [
            (1, "Mono"),
            (2, "Stereo"),
            (3, "Stereo 2.1"),
            (6, "Surround 5.1"),
            (8, "Surround 7.1"),
            (9, "Surround 7.2"),
            (0, ""),
            (99, ""),
        ],
    )
    def test_generate_track_name(self, channels, expected):
        assert AudioStream(channels=channels).generate_track_name() == expected

    def test_overall_bitrate_is_per_channel_times_channels(self):
        assert AudioStream(channel_bitrate=64, channels=6).overall_bitrate == 384


# ---------------------------------------------------------------------------
# FileInfo - path resolution
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Analyzer._get_ffprobe_result
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Analyzer._get_file_info - ffprobe payload parsing
# ---------------------------------------------------------------------------


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

    def test_video_bitrate_converted_to_kbps(self, media_file):
        assert self._info(media_file, video_bitrate=2_000_000).video_stream.bitrate == 2000

    def test_audio_codec_extracted(self, media_file):
        assert self._info(media_file, audio_codec="ac3").audio_streams[0].codec == "ac3"

    def test_audio_channels_extracted(self, media_file):
        assert self._info(media_file, audio_channels=6).audio_streams[0].channels == 6

    def test_audio_channel_bitrate_calculated(self, media_file):
        info = self._info(media_file, audio_bitrate=192_000, audio_channels=2)
        assert info.audio_streams[0].channel_bitrate == 96  # 192000 // 1000 // 2

    def test_audio_language_extracted(self, media_file):
        assert self._info(media_file, audio_lang="cze").audio_streams[0].lang == "cze"

    def test_no_audio_streams_on_video_only_file(self, media_file):
        payload = json.dumps(
            {
                "format": {"format_name": "mp4", "duration": "100.0"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "bit_rate": "1000000"}],
            }
        )
        assert analyze(media_file, payload, method=Analyzer._get_file_info).audio_streams == []


# ---------------------------------------------------------------------------
# Analyzer.analyze - shall_encode logic
# ---------------------------------------------------------------------------


class TestAnalyze:
    def _analyze(self, media_file: Path, **kwargs) -> FileInfo:
        return analyze(media_file, make_ffprobe_output(**kwargs))

    def test_no_encode_needed_when_all_within_limits(self, media_file):
        info = self._analyze(media_file)
        assert info.shall_encode is False
        assert info.video_stream.shall_encode is False
        assert all(not s.shall_encode for s in info.audio_streams)

    def test_encode_needed_wrong_container(self, media_file):
        assert self._analyze(media_file, container="avi").shall_encode is True

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


# ---------------------------------------------------------------------------
# Encoder.process - orchestration
# ---------------------------------------------------------------------------


class TestEncoderProcess:
    def _file_info(self, media_file: Path, **kwargs) -> FileInfo:
        return FileInfo(
            media_file,
            is_valid=True,
            duration=100,
            containers=["mp4"],
            video_stream=VideoStream(codec="h264", bitrate=2000, shall_encode=kwargs.get("video_shall_encode", False)),
            audio_streams=[AudioStream(codec="aac", channels=2, shall_encode=kwargs.get("audio_shall_encode", False))],
            shall_encode=kwargs.get("shall_encode", False),
        )

    def _process(self, encoder: Encoder, file_info: FileInfo, media_file: Path, **mocks):
        with (
            patch.object(Analyzer, "analyze", return_value=file_info),
            patch.object(encoder, "_encode", **mocks.get("encode", {"return_value": True})) as mock_encode,
            patch.object(encoder, "_optimize", **mocks.get("optimize", {"return_value": True})) as mock_optimize,
            patch.object(encoder, "_archive_original") as mock_archive,
            patch.object(encoder, "_cleanup_workdirs") as mock_cleanup,
        ):
            encoder.process(media_file)
        return mock_encode, mock_optimize, mock_archive, mock_cleanup

    def test_skips_encode_when_not_needed(self, media_file, special_dirs):
        encoder = Encoder()
        encode, optimize, archive, _ = self._process(encoder, self._file_info(media_file), media_file)
        encode.assert_not_called()
        optimize.assert_called_once_with(False)
        archive.assert_called_once()

    def test_calls_encode_when_needed(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, shall_encode=True)
        encode, optimize, archive, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()
        optimize.assert_called_once_with(True)
        archive.assert_called_once()

    def test_encode_triggered_by_video_stream(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, video_shall_encode=True)
        encode, _, _, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()

    def test_encode_triggered_by_audio_stream(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, audio_shall_encode=True)
        encode, _, _, _ = self._process(encoder, info, media_file)
        encode.assert_called_once()

    def test_aborts_without_archiving_when_encode_fails(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, shall_encode=True)
        _, optimize, archive, cleanup = self._process(encoder, info, media_file, encode={"return_value": False})
        optimize.assert_not_called()
        archive.assert_not_called()
        cleanup.assert_called_once()

    def test_aborts_without_archiving_when_optimize_fails(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, shall_encode=True)
        _, _, archive, cleanup = self._process(encoder, info, media_file, optimize={"return_value": False})
        archive.assert_not_called()
        cleanup.assert_called_once()

    def test_cleans_up_workdirs_even_when_a_step_raises(self, media_file, special_dirs):
        encoder = Encoder()
        info = self._file_info(media_file, shall_encode=True)
        with (
            patch.object(Analyzer, "analyze", return_value=info),
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


# ---------------------------------------------------------------------------
# Encoder._encode
# ---------------------------------------------------------------------------


class TestEncoderEncode:
    def _encoder(self, media_file: Path, **kwargs) -> Encoder:
        encoder = Encoder()
        encoder._processing_file = FileInfo(
            media_file,
            is_valid=kwargs.get("is_valid", True),
            duration=100,
            video_stream=VideoStream(codec="h264", bitrate=kwargs.get("video_bitrate", 5000)),
            audio_streams=kwargs.get("audio_streams", [AudioStream(codec="aac", channels=2, channel_bitrate=128)]),
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

    def test_returns_false_when_no_audio_streams(self, media_file, special_dirs):
        encoder = self._encoder(media_file, audio_streams=[])
        with patch("baycone.encoding.subprocess.Popen") as mock_popen:
            assert encoder._encode() is False
        mock_popen.assert_not_called()

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

    def test_audio_bitrate_is_per_channel_times_channels(self, media_file, special_dirs, location_config):
        location_config.target_audio_channel_bitrate = 64
        encoder = self._encoder(media_file, audio_streams=[AudioStream(codec="aac", channels=6, channel_bitrate=128)])
        with (
            patch("baycone.encoding.subprocess.Popen", side_effect=[make_popen_mock(), make_popen_mock()]),
            patch("baycone.encoding.ffmpeg.get_cmd_encode_movie_pass_2", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._encode()
        assert mock_cmd.call_args.args[4] == 384  # 64 * 6

    def test_logs_progress_without_dividing_by_zero_duration(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        encoder._processing_file.duration = 0
        progress = make_popen_mock(lines=["out_time_us=5000000\n", "progress=continue\n"])
        with patch("baycone.encoding.subprocess.Popen", side_effect=[progress, make_popen_mock()]):
            assert encoder._encode() is True


# ---------------------------------------------------------------------------
# Encoder._optimize
# ---------------------------------------------------------------------------


class TestEncoderOptimize:
    def _encoder(self, media_file: Path, audio_streams=None) -> Encoder:
        encoder = Encoder()
        encoder._processing_file = FileInfo(
            media_file,
            is_valid=True,
            duration=100,
            audio_streams=[AudioStream(codec="aac", channels=2)] if audio_streams is None else audio_streams,
        )
        return encoder

    def test_returns_true_on_success(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with patch("baycone.encoding.subprocess.run", return_value=MagicMock(returncode=0)):
            assert encoder._optimize(encoded=False) is True

    def test_returns_false_when_no_audio_streams(self, media_file, special_dirs):
        encoder = self._encoder(media_file, audio_streams=[])
        assert encoder._optimize(encoded=False) is False

    def test_returns_false_on_ffmpeg_failure(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with patch(
            "baycone.encoding.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "ffmpeg", stderr="error"),
        ):
            assert encoder._optimize(encoded=False) is False

    def test_reads_the_original_when_not_encoded(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.run"),
            patch("baycone.encoding.ffmpeg.get_cmd_optimize", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._optimize(encoded=False)
        assert mock_cmd.call_args.args[0] == media_file

    def test_reads_the_encoded_file_when_encoded(self, media_file, special_dirs):
        encoder = self._encoder(media_file)
        with (
            patch("baycone.encoding.subprocess.run"),
            patch("baycone.encoding.ffmpeg.get_cmd_optimize", return_value=["ffmpeg"]) as mock_cmd,
        ):
            encoder._optimize(encoded=True)
        assert mock_cmd.call_args.args[0] == encoder._processing_file.encoding_path


# ---------------------------------------------------------------------------
# Encoder._archive_original / directory pruning
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Encoder._cleanup_workdirs
# ---------------------------------------------------------------------------


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
