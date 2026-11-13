import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone.audio import AudioStream
from baycone.encoding import FileInfo, Job
from baycone.errors import EncodingError
from baycone_cli import cli

SERVICE_FILES = [Path(".baycone/config.override.yml"), Path(".baycone/state.json"), Path(".baycone/passlogfiles/x")]


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    media_file = tmp_path / "movie.mkv"
    media_file.write_bytes(b"")
    return media_file


def run_cli(*args: str, process: MagicMock | None = None) -> tuple[int, MagicMock]:
    """The exit code and the mocked `Encoder` class."""
    encoder = MagicMock()
    if process:
        encoder.return_value.process = process
    with patch("baycone_cli.cli.Encoder", encoder), patch("baycone_cli.cli.logging.basicConfig"):
        code = cli.main(list(args))
    return code, encoder


def job_of(encoder: MagicMock) -> Job:
    return encoder.return_value.process.call_args.args[0]


class TestArguments:
    def test_missing_input_is_rejected(self, tmp_path):
        code, encoder = run_cli(str(tmp_path / "missing.mkv"))
        assert code == 1
        encoder.assert_not_called()

    @pytest.mark.parametrize("args", [["-v", "0"], ["-a", "-1"], ["-l", "english"], ["--codec", "vp9"]])
    def test_invalid_values_are_rejected(self, media_file, args):
        with pytest.raises(SystemExit):
            run_cli(str(media_file), *args)

    def test_settings_follow_the_flags(self, media_file):
        _, encoder = run_cli(
            str(media_file), "-l", "cze", "-l", "eng", "-v", "1800", "-a", "96", "--codec", "hevc", "--subtitles", "eng"
        )
        settings, tools = encoder.call_args.args
        assert settings.target_audio_languages == ["cze", "eng"]
        assert (settings.target_video_bitrate, settings.target_audio_channel_bitrate) == (1800, 96)
        assert settings.target_video_codec == "hevc"
        assert settings.target_subtitle_languages == ["eng"]

    def test_languages_default_to_the_service_defaults(self, media_file):
        _, encoder = run_cli(str(media_file))
        assert encoder.call_args.args[0].target_audio_languages == ["original", "eng", "cze"]

    def test_binaries_are_configurable(self, media_file):
        _, encoder = run_cli(str(media_file), "--ffmpeg", "/opt/ffmpeg", "--ffprobe", "/opt/ffprobe")
        tools = encoder.call_args.args[1]
        assert (tools.ffmpeg, tools.ffprobe) == ("/opt/ffmpeg", "/opt/ffprobe")


class TestJob:
    def test_output_defaults_next_to_the_input(self, media_file):
        _, encoder = run_cli(str(media_file))
        assert job_of(encoder).output_file == media_file.parent / "_baycone_processed" / "movie.mp4"

    def test_output_is_taken_as_given(self, media_file, tmp_path):
        _, encoder = run_cli(str(media_file), "--output", str(tmp_path / "out" / "film.mp4"))
        assert job_of(encoder).output_file == tmp_path / "out" / "film.mp4"

    def test_remux_only_is_passed_on(self, media_file):
        _, encoder = run_cli(str(media_file), "--remux-only")
        assert encoder.return_value.process.call_args.kwargs == {"remux_only": True}

    def test_input_is_never_touched(self, media_file):
        media_file.write_bytes(b"movie")
        run_cli(str(media_file))
        assert media_file.read_bytes() == b"movie"


class TestWorkDir:
    def test_default_work_dir_is_removed(self, media_file):
        def process(job, remux_only):
            (job.work_dir / "encoded").mkdir(parents=True)

        run_cli(str(media_file), process=MagicMock(side_effect=process))
        assert not (media_file.parent / ".baycone").exists()

    def test_default_work_dir_is_removed_on_failure(self, media_file):
        def process(job, remux_only):
            job.work_dir.mkdir(parents=True)
            raise EncodingError("Encoding failed")

        code, _ = run_cli(str(media_file), process=MagicMock(side_effect=process))
        assert code == 2
        assert not (media_file.parent / ".baycone").exists()

    @pytest.mark.parametrize("fails", [False, True], ids=["success", "failure"])
    def test_keeps_the_files_of_a_service_watching_the_directory(self, media_file, fails):
        """Its work dir used to be `.baycone` itself, wiped before and after every run."""
        kept = [media_file.parent / path for path in SERVICE_FILES]
        for file_path in kept:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("service")

        process = MagicMock(side_effect=EncodingError("Encoding failed") if fails else None)
        run_cli(str(media_file), process=process)

        assert all(file_path.read_text() == "service" for file_path in kept)
        assert not (media_file.parent / ".baycone" / "cli").exists()

    def test_explicit_work_dir_is_left_alone(self, media_file, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        run_cli(str(media_file), "--work-dir", str(work_dir))
        assert work_dir.is_dir()


class TestErrors:
    def test_processing_error_exits_with_2(self, media_file, capsys):
        code, _ = run_cli(str(media_file), process=MagicMock(side_effect=EncodingError("Encoding failed")))
        assert code == 2
        assert "Error: Encoding failed" in capsys.readouterr().err

    def test_unexpected_error_exits_with_2(self, media_file):
        code, _ = run_cli(str(media_file), process=MagicMock(side_effect=OSError("disk full")))
        assert code == 2


class TestInfo:
    def test_prints_the_analysis_as_json(self, media_file, capsys):
        info = FileInfo(media_file, is_valid=True, duration=10, audio_streams=[AudioStream(lang="ces")])
        with patch("baycone_cli.cli.Analyzer") as analyzer:
            analyzer.return_value.analyze.return_value = info
            code, encoder = run_cli(str(media_file), "--info")
        printed = json.loads(capsys.readouterr().out)
        assert code == 0
        assert printed["duration"] == 10
        assert printed["audio_streams"][0]["lang"] == "ces"
        encoder.assert_not_called()

    def test_invalid_file_exits_with_2(self, media_file):
        with patch("baycone_cli.cli.Analyzer") as analyzer:
            analyzer.return_value.analyze.return_value = FileInfo(media_file)
            code, _ = run_cli(str(media_file), "--info")
        assert code == 2


class TestConsole:
    def test_log_record_ends_the_progress_line(self, capsys):
        console = cli.Console()
        console.progress("Optimizing", 42.0)
        cli.ConsoleHandler(console).emit(
            MagicMock(getMessage=lambda: "done", exc_info=None, exc_text=None, stack_info=None)
        )
        assert capsys.readouterr().err.startswith("\rOptimizing:  42.0 %\n")
