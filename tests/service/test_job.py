import multiprocessing
import runpy
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone.errors import EncodingError, InsufficientSpaceError
from baycone.settings import FFmpegTools
from baycone_service.config import SpecialDirs
from baycone_service.job import JobResult, _interrupt, process_file, run_job
from baycone_service.layout import LocatedFile, Slot

TOOLS = FFmpegTools()


@pytest.fixture
def special_dirs(location_config) -> SpecialDirs:
    dirs = SpecialDirs(location_config.abspath)
    dirs.create()
    return dirs


@pytest.fixture
def located(location_dir, special_dirs) -> LocatedFile:
    video = location_dir / "movie.mp4"
    video.touch()
    return LocatedFile.resolve(video)


class TestProcessFile:
    def _process(self, located: LocatedFile, slot: Slot = Slot.ENCODE, **process) -> tuple[JobResult, MagicMock]:
        encoder = MagicMock()
        encoder.process.configure_mock(**process)
        with (
            patch("baycone_service.job.Encoder", return_value=encoder),
            patch("baycone_service.job.archive_original") as archive,
        ):
            result = process_file(located, TOOLS, slot)
        return result, archive

    def test_archives_the_original_once_published(self, located):
        result, archive = self._process(located)
        assert result is JobResult.DONE
        archive.assert_called_once_with(located)

    def test_failed_file_is_never_archived(self, located, caplog):
        result, archive = self._process(located, side_effect=EncodingError("Encoding failed"))
        assert result is JobResult.FAILED
        archive.assert_not_called()
        assert "Processing aborted: Encoding failed" in caplog.text

    def test_waiting_for_space_is_not_a_failure(self, located):
        result, archive = self._process(located, side_effect=InsufficientSpaceError("needs 10.0 GiB"))
        assert result is JobResult.NO_SPACE
        archive.assert_not_called()

    def test_encodes_with_the_location_settings_and_the_given_binaries(self, located, location_config):
        location_config.target_video_bitrate = 1500
        tools = FFmpegTools(ffmpeg="/opt/ffmpeg")
        with patch("baycone_service.job.Encoder") as encoder, patch("baycone_service.job.archive_original"):
            process_file(located, tools, Slot.ENCODE)
        settings, given_tools = encoder.call_args.args
        assert settings.target_video_bitrate == 1500
        assert given_tools is tools

    @pytest.mark.parametrize("slot", list(Slot))
    def test_works_in_the_slot_work_dir(self, located, slot):
        encoder = MagicMock()
        with patch("baycone_service.job.Encoder", return_value=encoder), patch("baycone_service.job.archive_original"):
            process_file(located, TOOLS, slot)
        assert encoder.process.call_args.args[0].work_dir == located.job(slot).work_dir


class TestRunJob:
    def _run(self, located: LocatedFile, **process_file) -> int:
        # Never for real: `setpgrp` would detach pytest itself from the terminal's process group
        with (
            patch("baycone_service.job.setup_logging") as setup_logging,
            patch("baycone_service.job.signal.signal"),
            patch("baycone_service.job.os.setpgrp", create=True) as setpgrp,
            patch("baycone_service.job.process_file", **process_file),
            pytest.raises(SystemExit) as exit_,
        ):
            run_job(located, TOOLS, Slot.COPY)
        setup_logging.assert_called_once_with(Slot.COPY)
        setpgrp.assert_called_once_with()
        return exit_.value.code

    def test_exits_with_the_result(self, located):
        assert self._run(located, return_value=JobResult.NO_SPACE) == JobResult.NO_SPACE

    def test_stop_is_not_a_failure(self, located):
        assert self._run(located, side_effect=KeyboardInterrupt) == JobResult.STOPPED

    def test_unexpected_error_fails_the_file(self, located, caplog):
        assert self._run(located, side_effect=OSError("disk full")) == JobResult.FAILED
        assert "Processing failed" in caplog.text

    def test_first_signal_interrupts_and_later_ones_are_ignored(self):
        """A second Ctrl+C must not cut the cleanup short the first one started."""
        with patch("baycone_service.job.signal.signal") as set_handler, pytest.raises(KeyboardInterrupt):
            _interrupt(signal.SIGTERM, None)
        assert {call.args for call in set_handler.call_args_list} == {
            (signal.SIGINT, signal.SIG_IGN),
            (signal.SIGTERM, signal.SIG_IGN),
        }


class TestSpawnedJob:
    def test_runs_in_a_spawned_process(self, located, special_dirs):
        """The service's real path: the arguments are pickled, the modules imported anew, the result an exit code."""
        located.file_path.write_bytes(b"not a video")
        tools = FFmpegTools(ffmpeg="baycone-missing-ffmpeg", ffprobe="baycone-missing-ffprobe")
        process = multiprocessing.get_context("spawn").Process(target=run_job, args=(located, tools, Slot.COPY))
        process.start()
        process.join(60)
        assert process.exitcode == JobResult.FAILED
        assert located.file_path.is_file()
        assert not any(path.is_file() for path in special_dirs.copy_dir.rglob("*"))


class TestMainModule:
    def test_does_not_start_a_service_when_a_job_process_imports_it(self):
        """Spawn imports the parent's main module as `__mp_main__` in every child."""
        with patch("baycone_service.service.main") as main:
            runpy.run_module("baycone_service.__main__", run_name="__mp_main__")
        main.assert_not_called()

    def test_starts_the_service_when_run(self):
        with patch("baycone_service.service.main", return_value=0) as main, pytest.raises(SystemExit):
            runpy.run_module("baycone_service.__main__", run_name="__main__")
        main.assert_called_once()
