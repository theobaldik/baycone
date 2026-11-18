import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone_service.config import Constants, config
from baycone_service.job import JobResult
from baycone_service.layout import Slot
from baycone_service.service import STOP_TIMEOUT, Service, _kill_group
from baycone_service.watcher import FileChangeTracker


class FakeProcess:
    """A job process; `exitcode` stays None while it runs."""

    pids = iter(range(900_001, 901_000))

    def __init__(self, target=None, args=(), name=None) -> None:
        self.args, self.name = args, name
        self.pid = next(self.pids)
        self.exitcode: int | None = None
        self.sentinel = object()
        self.started = self.terminated = self.killed = self.closed = False
        self.exit_on_terminate: int | None = JobResult.STOPPED

    def start(self) -> None:
        self.started = True

    def terminate(self) -> None:
        """Like the real one, a no-op for a process that already exited."""
        self.terminated = True
        if self.exitcode is None:
            self.exitcode = self.exit_on_terminate

    def join(self, timeout: float | None = None) -> None:
        pass

    def is_alive(self) -> bool:
        return self.started and self.exitcode is None

    def kill(self) -> None:
        self.killed = True
        self.exitcode = -9

    def close(self) -> None:
        self.closed = True


# Taken before any test patches it: the patch replaces it in the `os` module itself
REAL_KILLPG = getattr(os, "killpg", None)


@pytest.fixture(autouse=True)
def killpg() -> MagicMock:
    """The fake pids must never reach the real `os.killpg`."""
    with patch("baycone_service.service.os.killpg") as killpg:
        yield killpg


@pytest.fixture
def processes() -> list[FakeProcess]:
    return []


@pytest.fixture
def service(processes) -> Service:
    service = Service()
    service._context = MagicMock()

    def make_process(**kwargs) -> FakeProcess:
        processes.append(FakeProcess(**kwargs))
        return processes[-1]

    service._context.Process.side_effect = make_process
    with patch("baycone_service.service.LocatedFile.resolve", side_effect=lambda path: f"located:{path.name}"):
        yield service


def bump_mtime(file_path: Path) -> None:
    stat = file_path.stat()
    os.utime(file_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def make_watcher(files: dict[str, Slot] | None = None, root: Path = Path("/locations/movies")) -> MagicMock:
    """Ready files by name, with the slot each one takes."""
    watcher = MagicMock()
    watcher.files = {root / name: slot for name, slot in (files or {}).items()}
    watcher.scan.side_effect = lambda: [path for path in watcher.files if path not in watcher.running]
    watcher.running = set()
    watcher.slot_of.side_effect = lambda path: watcher.files[path]

    def begin(path: Path) -> bool:
        watcher.running.add(path)
        return True

    watcher.begin.side_effect = begin
    return watcher


def started(processes: list[FakeProcess]) -> list[tuple[str, Slot]]:
    """(located file, slot) of every job process started."""
    return [(process.args[0], process.args[2]) for process in processes if process.started]


class TestDispatch:
    def test_idle_when_no_file_is_ready(self, service, processes):
        service._watchers = [make_watcher()]
        service._dispatch()
        assert processes == []

    def test_encode_and_copy_run_together(self, service, processes):
        service._watchers = [make_watcher({"movie.mkv": Slot.ENCODE, "clip.mkv": Slot.COPY})]
        service._dispatch()
        assert started(processes) == [("located:movie.mkv", Slot.ENCODE), ("located:clip.mkv", Slot.COPY)]
        assert [process.name for process in processes] == ["baycone-encode", "baycone-copy"]

    def test_second_encode_waits_while_copies_keep_flowing(self, service, processes):
        watcher = make_watcher({"a.mkv": Slot.ENCODE, "b.mkv": Slot.ENCODE, "c.mkv": Slot.COPY})
        service._watchers = [watcher]
        service._dispatch()

        processes[1].exitcode = JobResult.DONE
        del watcher.files[Path("/locations/movies/c.mkv")]
        watcher.files[Path("/locations/movies/d.mkv")] = Slot.COPY
        service._collect_finished()
        service._dispatch()

        assert started(processes) == [
            ("located:a.mkv", Slot.ENCODE),
            ("located:c.mkv", Slot.COPY),
            ("located:d.mkv", Slot.COPY),
        ]
        assert Path("/locations/movies/b.mkv") not in watcher.running

    def test_freed_encode_slot_takes_the_waiting_file(self, service, processes):
        watcher = make_watcher({"a.mkv": Slot.ENCODE, "b.mkv": Slot.ENCODE})
        service._watchers = [watcher]
        service._dispatch()
        processes[0].exitcode = JobResult.DONE
        del watcher.files[Path("/locations/movies/a.mkv")]
        service._collect_finished()
        service._dispatch()
        assert started(processes)[-1] == ("located:b.mkv", Slot.ENCODE)

    def test_no_scan_while_both_slots_are_busy(self, service):
        watcher = make_watcher({"a.mkv": Slot.ENCODE, "b.mkv": Slot.COPY})
        service._watchers = [watcher]
        service._dispatch()
        service._dispatch()
        watcher.scan.assert_called_once()

    def test_slots_are_shared_by_every_location(self, service, processes):
        first = make_watcher({"a.mkv": Slot.ENCODE}, root=Path("/locations/movies"))
        second = make_watcher({"b.mkv": Slot.ENCODE, "c.mkv": Slot.COPY}, root=Path("/locations/shows"))
        service._watchers = [first, second]
        service._dispatch()
        assert started(processes) == [("located:a.mkv", Slot.ENCODE), ("located:c.mkv", Slot.COPY)]

    def test_file_given_up_on_is_skipped(self, service, processes):
        watcher = make_watcher({"a.mkv": Slot.COPY, "b.mkv": Slot.COPY})
        watcher.begin.side_effect = lambda path: path.name != "a.mkv"
        service._watchers = [watcher]
        service._dispatch()
        assert started(processes) == [("located:b.mkv", Slot.COPY)]

    def test_job_that_cannot_be_started_fails_the_file_and_frees_the_slot(self, service, processes, caplog):
        watcher = make_watcher({"a.mkv": Slot.COPY})
        service._watchers = [watcher]
        service._context.Process.side_effect = OSError("too many processes")
        service._dispatch()
        watcher.finish.assert_called_once_with(Path("/locations/movies/a.mkv"), failed=True)
        assert service._jobs == {}
        assert "could not be started" in caplog.text

    def test_job_gets_the_configured_binaries(self, service, processes):
        service._watchers = [make_watcher({"a.mkv": Slot.COPY})]
        with patch.object(config, "ffmpeg_path", "/opt/ffmpeg"):
            service._dispatch()
        assert processes[0].args[1].ffmpeg == "/opt/ffmpeg"


class TestCollectFinished:
    VIDEO = Path("/locations/movies/a.mkv")

    def _finish_with(self, service, processes, exitcode: int | None) -> MagicMock:
        watcher = make_watcher({"a.mkv": Slot.ENCODE})
        service._watchers = [watcher]
        service._dispatch()
        processes[0].exitcode = exitcode
        service._collect_finished()
        return watcher

    def test_running_job_keeps_its_slot(self, service, processes):
        watcher = self._finish_with(service, processes, None)
        assert Slot.ENCODE in service._jobs
        watcher.finish.assert_not_called()

    @pytest.mark.parametrize(
        "exitcode, failed",
        [(JobResult.DONE, False), (JobResult.FAILED, True), (JobResult.STOPPED, False)],
        ids=["done", "failed", "stopped"],
    )
    def test_result_closes_the_attempt(self, service, processes, exitcode, failed):
        watcher = self._finish_with(service, processes, exitcode)
        watcher.finish.assert_called_once_with(self.VIDEO, failed=failed)
        assert service._jobs == {}
        assert processes[0].closed

    def test_waiting_for_space_pauses_the_location_without_failing(self, service, processes):
        watcher = self._finish_with(service, processes, JobResult.NO_SPACE)
        watcher.pause.assert_called_once()
        watcher.finish.assert_called_once_with(self.VIDEO, failed=False)

    @pytest.mark.parametrize("exitcode", [-9, 1], ids=["killed", "crashed"])
    def test_job_that_died_leaves_the_attempt_open(self, service, processes, exitcode, caplog):
        """Counted like a service that died, so a file that keeps killing its job is given up on."""
        watcher = self._finish_with(service, processes, exitcode)
        watcher.abandon.assert_called_once_with(self.VIDEO)
        watcher.finish.assert_not_called()
        assert f"died with exit code {exitcode}" in caplog.text

    def test_ffmpeg_of_a_job_that_died_is_killed(self, service, processes, killpg):
        """It would go on writing into the work dirs, where the next attempt is about to start."""
        self._finish_with(service, processes, -9)
        killpg.assert_called_once_with(processes[0].pid, signal.SIGKILL)

    @pytest.mark.parametrize("exitcode", list(JobResult))
    def test_job_that_cleaned_up_is_not_killed(self, service, processes, killpg, exitcode):
        self._finish_with(service, processes, exitcode)
        killpg.assert_not_called()

    def test_job_group_that_is_already_gone_is_fine(self, service, processes, killpg):
        killpg.side_effect = ProcessLookupError
        self._finish_with(service, processes, -9)


class TestStop:
    def _running(self, service, processes) -> MagicMock:
        watcher = make_watcher({"a.mkv": Slot.ENCODE, "b.mkv": Slot.COPY})
        service._watchers = [watcher]
        service._dispatch()
        return watcher

    def test_interrupts_the_jobs_without_failing_their_files(self, service, processes):
        watcher = self._running(service, processes)
        service.stop()
        assert all(process.terminated and not process.killed for process in processes)
        assert {call.args for call in watcher.finish.call_args_list} == {
            (Path("/locations/movies/a.mkv"),),
            (Path("/locations/movies/b.mkv"),),
        }
        assert all(call.kwargs == {"failed": False} for call in watcher.finish.call_args_list)
        assert service._jobs == {}

    def test_kills_a_job_that_does_not_stop_in_time(self, service, processes, killpg, caplog):
        watcher = self._running(service, processes)
        processes[0].exit_on_terminate = None
        service.stop()
        assert processes[0].killed and not processes[1].killed
        killpg.assert_called_once_with(processes[0].pid, signal.SIGKILL)
        watcher.finish.assert_any_call(Path("/locations/movies/a.mkv"), failed=False)
        assert "did not stop in time" in caplog.text

    def test_waits_at_most_the_timeout_for_all_jobs_together(self, service, processes):
        self._running(service, processes)
        timeouts = []
        for process in processes:
            process.join = lambda timeout=None: timeouts.append(timeout)
        with patch("baycone_service.service.time.monotonic", side_effect=[100.0, 100.0, 105.0]):
            service.stop()
        assert timeouts == [STOP_TIMEOUT, STOP_TIMEOUT - 5.0]

    def test_job_that_failed_meanwhile_still_fails_its_file(self, service, processes):
        watcher = self._running(service, processes)
        processes[1].exitcode = JobResult.FAILED
        service.stop()
        watcher.finish.assert_any_call(Path("/locations/movies/b.mkv"), failed=True)

    def test_stop_ends_the_main_loop(self, service):
        service.stop()
        with patch.object(service, "_dispatch") as dispatch:
            service.main_loop()
        dispatch.assert_not_called()


class TestMainLoop:
    def _loop(self, service, iterations: list) -> None:
        """Runs one iteration per entry of `iterations`: an exception to raise from `_dispatch`, or None."""
        steps = iter(iterations)

        def dispatch() -> None:
            step = next(steps, StopIteration)
            if step is StopIteration:
                service._stop_signal = True
            elif step is not None:
                raise step

        with patch.object(service, "_reload_config"), patch.object(service, "_dispatch", side_effect=dispatch):
            service.main_loop()

    def test_sleeps_the_poll_interval_when_idle(self, service):
        with patch("baycone_service.service.time.sleep") as sleep:
            self._loop(service, [None])
        assert sleep.call_args_list[0].args == (config.poll_interval,)

    def test_waits_for_a_job_to_end_or_the_poll_interval(self, service, processes):
        """A job that ends frees its slot at once, instead of after the rest of the poll interval."""
        service._watchers = [make_watcher({"a.mkv": Slot.ENCODE})]
        service._dispatch()
        with (
            patch("baycone_service.service.wait") as wait,
            patch("baycone_service.service.time.sleep") as sleep,
        ):
            self._loop(service, [None])
        assert wait.call_args.args == ([processes[0].sentinel],)
        assert wait.call_args.kwargs == {"timeout": config.poll_interval}
        sleep.assert_not_called()

    def test_survives_an_unexpected_error(self, service, caplog):
        with patch("baycone_service.service.time.sleep") as sleep:
            self._loop(service, [RuntimeError("boom"), None])
        # Waited after the error too, a failure that repeats must not spin
        assert sleep.call_count == 3
        assert "boom" in caplog.text

    def test_stop_signal_is_not_swallowed(self, service):
        with pytest.raises(KeyboardInterrupt):
            self._loop(service, [KeyboardInterrupt()])

    def test_collects_finished_jobs_before_dispatching(self, service):
        calls = []
        with (
            patch.object(service, "_reload_config", side_effect=lambda: calls.append("reload")),
            patch.object(service, "_collect_finished", side_effect=lambda: calls.append("collect")),
            patch.object(service, "_dispatch", side_effect=lambda: (calls.append("dispatch"), service.stop())),
            patch("baycone_service.service.time.sleep"),
        ):
            service.main_loop()
        assert calls == ["reload", "collect", "dispatch"]


class TestReloadConfig:
    @pytest.fixture
    def config_file(self, service, locations_root) -> Path:
        """The service started with a single 'movies' location."""
        config_file = locations_root / "config.yml"
        config_file.write_text("locations:\n  - path: movies\n")
        service._config_file = FileChangeTracker(config_file)
        service._config_file.changed()
        service._load_config()
        return config_file

    def test_unchanged_file_is_not_reloaded(self, service, config_file):
        with patch.object(service, "_load_config") as mock_load:
            service._reload_config()
        mock_load.assert_not_called()

    def _reload(self, config_file: Path, content: str) -> None:
        config_file.write_text(content)
        bump_mtime(config_file)

    def test_changed_file_adds_a_watcher_for_a_new_location(self, service, config_file):
        movies = service._watchers[0]
        self._reload(config_file, "locations:\n  - path: movies\n  - path: shows\n")
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies", "shows"]
        assert service._watchers[0] is movies
        assert service._watchers[1].dir_path == config.locations[1].abspath

    def test_location_keeps_its_watcher_with_the_new_config(self, service, config_file):
        """A new one would clear the work dirs, under the feet of a job running there."""
        movies = service._watchers[0]
        encoded = movies.special_dirs.encoded_dir / "movie.mp4"
        encoded.touch()
        self._reload(config_file, "defaults:\n  target_video_bitrate: 777\nlocations:\n  - path: movies\n")
        service._reload_config()
        assert service._watchers == [movies]
        assert movies._location_config is config.locations[0]
        assert config.locations[0].target_video_bitrate == 777
        assert encoded.exists()

    def test_removed_location_is_no_longer_watched(self, service, config_file):
        self._reload(config_file, "locations: []\n")
        service._reload_config()
        assert service._watchers == []

    def test_location_readded_while_its_job_runs_gets_that_job_s_watcher(self, service, config_file):
        movies = service._watchers[0]
        service._jobs[Slot.ENCODE] = MagicMock(watcher=movies)
        self._reload(config_file, "locations: []\n")
        service._reload_config()
        self._reload(config_file, "locations:\n  - path: movies\n")
        service._reload_config()
        assert service._watchers == [movies]

    def test_reload_retries_failed_files(self, service, config_file, locations_root):
        """The reload may be the fix, and it is the way to ask for a retry: `touch config.yml`."""
        video = locations_root / "movies" / "movie.mp4"
        video.touch()
        watcher = service._watchers[0]
        watcher.scan()
        watcher.begin(video)
        watcher.finish(video, failed=True)
        assert watcher.scan() == []

        bump_mtime(config_file)
        service._reload_config()

        assert service._watchers[0].scan() == [video]

    @pytest.mark.parametrize(
        "content",
        ["- just\n- a list\n", "poll_interval: soon\n", "locations: movies\n", "defaults:\n  allowed_extensions: 5\n"],
        ids=["list", "wrong type", "locations not a list", "wrong list type"],
    )
    def test_wrongly_typed_file_keeps_the_previous_config(self, service, config_file, content):
        """Such a value would only blow up later, somewhere in the main loop."""
        watchers = service._watchers
        config_file.write_text(content)
        bump_mtime(config_file)
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert service._watchers is watchers

    def test_location_that_cannot_be_set_up_keeps_the_previous_config(self, service, config_file):
        """The config used to be swapped before the watchers were built, leaving the two out of step."""
        watchers = service._watchers
        defaults = config.defaults
        config_file.write_text("defaults:\n  target_video_bitrate: 777\nlocations:\n  - path: shows\n")
        bump_mtime(config_file)
        with patch("baycone_service.service.DirWatcher", side_effect=PermissionError("read-only")):
            service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert config.defaults is defaults
        assert service._watchers is watchers

    def test_invalid_file_keeps_the_previous_config(self, service, config_file):
        watchers = service._watchers
        config_file.write_text("locations: [\n")
        bump_mtime(config_file)
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert service._watchers is watchers

    def test_deleted_file_keeps_the_previous_config(self, service, config_file):
        config_file.unlink()
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert len(service._watchers) == 1


class TestStart:
    def test_fails_without_env_variables(self, service, monkeypatch):
        monkeypatch.delenv("BAYCONE_LOCATIONS", raising=False)
        monkeypatch.delenv("BAYCONE_CONFIG", raising=False)
        assert service.start() == 1

    @pytest.mark.parametrize(
        "content", [None, "locations: [\n", "poll_interval: soon\n"], ids=["missing", "invalid", "wrong type"]
    )
    def test_fails_on_unreadable_config(self, service, monkeypatch, tmp_path, content):
        config_file = tmp_path / "config.yml"
        if content is not None:
            config_file.write_text(content)
        monkeypatch.setenv("BAYCONE_LOCATIONS", str(tmp_path))
        monkeypatch.setenv("BAYCONE_CONFIG", str(config_file))
        monkeypatch.setattr(Constants, "FILE_CONFIG", str(config_file))
        assert service.start() == 1


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
class TestKillGroup:
    def test_kills_the_ffmpeg_a_killed_job_left_behind(self, killpg):
        """Only processes this test starts: a job in its own group, and a `sleep` standing in for its ffmpeg."""
        killpg.side_effect = REAL_KILLPG  # on a group this test owns
        job = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, subprocess, time; os.setpgrp();"
                "print(subprocess.Popen(['sleep', '60']).pid, flush=True); time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            orphan = int(job.stdout.readline())
            job.kill()
            job.wait()
            assert _alive(orphan)

            _kill_group(job.pid)

            deadline = time.monotonic() + 5
            while _alive(orphan) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not _alive(orphan)
        finally:
            # A failed assertion must not leave the test's own processes behind
            with suppress(ProcessLookupError):
                REAL_KILLPG(job.pid, signal.SIGKILL)
            job.stdout.close()


def _alive(pid: int) -> bool:
    """A zombie counts as gone: it is dead, only its parent has not collected it yet."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except FileNotFoundError, IndexError:
        return False
