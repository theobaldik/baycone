import argparse
import json
import signal
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
        assert code == 2
        encoder.assert_not_called()

    @pytest.mark.parametrize("args", [["-v", "0"], ["-a", "-1"], ["-l", "english"], ["--codec", "vp9"]])
    def test_invalid_values_are_rejected(self, media_file, args):
        with pytest.raises(SystemExit) as exit_:
            run_cli(str(media_file), *args)
        assert exit_.value.code == 2

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


class TestTimestamp:
    @pytest.mark.parametrize(
        "value, seconds", [("83", 83.0), ("1:23", 83.0), ("1:23:03.5", 4983.5), ("0.25", 0.25), ("00:00:07", 7.0)]
    )
    def test_parses_positions(self, value, seconds):
        assert cli._timestamp(value) == seconds

    @pytest.mark.parametrize("value", ["-5", "1:75", "1:60:00", "abc", "1:2:3:4", "", "5s"])
    def test_rejects_anything_else(self, value):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._timestamp(value)

    def test_help_still_exits_with_0(self):
        with pytest.raises(SystemExit) as exit_:
            cli.parse_args(["--help"])
        assert exit_.value.code == 0


class TestJob:
    def test_output_defaults_next_to_the_input(self, media_file):
        _, encoder = run_cli(str(media_file))
        assert job_of(encoder).output_file == media_file.parent / "_baycone_processed" / "movie.mp4"

    def test_output_is_taken_as_given(self, media_file, tmp_path):
        _, encoder = run_cli(str(media_file), "--output", str(tmp_path / "out" / "film.mp4"))
        assert job_of(encoder).output_file == tmp_path / "out" / "film.mp4"

    def test_cut_is_passed_on(self, media_file):
        job = job_of(run_cli(str(media_file), "--start", "0:05", "--end", "1:02:03.5")[1])
        assert (job.start, job.end) == (5.0, 3723.5)

    def test_no_cut_by_default(self, media_file):
        job = job_of(run_cli(str(media_file))[1])
        assert (job.start, job.end) == (None, None)

    def test_start_after_end_is_rejected_before_anything_runs(self, media_file, capsys):
        with patch("baycone_cli.cli.Analyzer") as analyzer, pytest.raises(SystemExit) as exit_:
            run_cli(str(media_file), "--start", "20", "--end", "10")
        assert exit_.value.code == 2
        analyzer.assert_not_called()
        assert "--start must be before --end" in capsys.readouterr().err

    def test_force_video_encode_is_passed_on(self, media_file):
        _, encoder = run_cli(str(media_file), "--force-video-encode")
        assert encoder.call_args.args[0].force_video_encode is True

    def test_force_video_encode_and_remux_only_exclude_each_other(self, media_file):
        with pytest.raises(SystemExit) as exit_:
            run_cli(str(media_file), "--force-video-encode", "--remux-only")
        assert exit_.value.code == 2

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
        assert code == 1
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
    def test_processing_error_exits_with_1(self, media_file, capsys):
        code, _ = run_cli(str(media_file), process=MagicMock(side_effect=EncodingError("Encoding failed")))
        assert code == 1
        assert "Error: Encoding failed" in capsys.readouterr().err

    def test_unexpected_error_exits_with_1(self, media_file):
        code, _ = run_cli(str(media_file), process=MagicMock(side_effect=OSError("disk full")))
        assert code == 1


class TestInterrupt:
    def test_exits_with_130_and_removes_the_work_dir(self, media_file):
        def process(job, remux_only):
            job.work_dir.mkdir(parents=True)
            raise KeyboardInterrupt

        code, _ = run_cli(str(media_file), process=MagicMock(side_effect=process))
        assert code == 130
        assert not (media_file.parent / ".baycone").exists()

    def test_restores_the_previous_handler(self, media_file):
        before = signal.getsignal(signal.SIGINT)
        run_cli(str(media_file))
        assert signal.getsignal(signal.SIGINT) is before

    def test_first_ctrl_c_unwinds_and_arms_the_force_stop(self, capsys):
        with patch("baycone_cli.cli.signal.signal") as set_handler, pytest.raises(KeyboardInterrupt):
            cli._interrupt(cli.Console(), signal.SIGINT, None)
        set_handler.assert_called_once_with(signal.SIGINT, cli._force_stop)
        assert "Ctrl+C again" in capsys.readouterr().err

    def test_second_ctrl_c_exits_without_cleanup(self):
        with patch("baycone_cli.cli.os._exit") as exit_now:
            cli._force_stop(signal.SIGINT, None)
        exit_now.assert_called_once_with(130)


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

    def test_invalid_file_exits_with_1(self, media_file):
        with patch("baycone_cli.cli.Analyzer") as analyzer:
            analyzer.return_value.analyze.return_value = FileInfo(media_file)
            code, _ = run_cli(str(media_file), "--info")
        assert code == 1


class TestConsole:
    def test_log_record_ends_the_progress_line(self, capsys):
        console = cli.Console()
        console.progress("Optimizing", 42.0)
        cli.ConsoleHandler(console).emit(
            MagicMock(getMessage=lambda: "done", exc_info=None, exc_text=None, stack_info=None)
        )
        assert capsys.readouterr().err.startswith("\rOptimizing:  42.0 %\n")
