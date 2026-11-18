import logging
import multiprocessing
import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, fields
from multiprocessing.connection import wait
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import FrameType

import yaml

from baycone_service import config
from baycone_service.job import JobResult, run_job
from baycone_service.layout import LocatedFile, Slot
from baycone_service.logger import setup_logging
from baycone_service.watcher import DirWatcher, FileChangeTracker

log = logging.getLogger(__name__)

# How long a stop waits for the jobs to clean up; within the 10 s `docker stop` grants before it kills
STOP_TIMEOUT: float = 8.0


def _kill_group(pid: int | None) -> None:
    """What is left of a job that died without its cleanup: an ffmpeg that would go on writing into the work dirs."""
    if pid is None or not hasattr(os, "killpg"):
        return
    # The group exists as long as any of its processes does, so its id cannot have been reused
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


@dataclass
class RunningJob:
    process: BaseProcess
    watcher: DirWatcher
    file_path: Path


class Service:
    """
    Dispatches the ready files of every location to the slots, each file in a child process of its own.

    Only this process touches the watchers and their state; a job only processes its file.
    """

    def __init__(self) -> None:
        self._watchers: list[DirWatcher] = []
        self._jobs: dict[Slot, RunningJob] = {}
        self._stop_signal: bool = False
        self._config_file: FileChangeTracker | None = None
        # Not fork: the children must not inherit the dispatcher's state, and it is what Windows and macOS have
        self._context = multiprocessing.get_context("spawn")

    def _load_config(self) -> None:
        """All or nothing: a location whose watcher cannot be set up leaves the previous config in place."""
        previous = {f.name: getattr(config.config, f.name) for f in fields(config.config)}
        try:
            config.load_config(self._config_file.file_path)
            self._watchers = self._reconcile_watchers()
        except Exception:
            for name, value in previous.items():
                setattr(config.config, name, value)
            raise

    def _reconcile_watchers(self) -> list[DirWatcher]:
        """A location keeps its watcher: a new one would clear the work dirs of a job running there."""
        existing = {w.dir_path: w for w in [*self._watchers, *(job.watcher for job in self._jobs.values())]}
        watchers, updates = [], []
        for lc in config.config.locations:
            if watcher := existing.get(lc.abspath):
                updates.append((watcher, lc))
            else:
                watcher = DirWatcher(lc)
            watchers.append(watcher)
        # Only once every new watcher could be set up, a failure leaves the previous config in place
        for watcher, lc in updates:
            watcher.update_config(lc)
        return watchers

    def _reload_config(self) -> None:
        """A failed reload keeps the previous config; it is retried once the file changes again."""
        if not self._config_file.changed():
            return
        try:
            self._load_config()
        except Exception as e:
            log.error(f"Configuration '{self._config_file.file_path}' not reloaded, keeping the previous one: {e}")
            return
        log.info(f"Configuration '{self._config_file.file_path}' reloaded")
        # The reload may be the fix for whatever made them fail
        for watcher in self._watchers:
            watcher.retry_failed()

    def _dispatch(self) -> None:
        """Fills the free slots; a file waiting for a busy slot does not hold back the ones behind it."""
        free = [slot for slot in Slot if slot not in self._jobs]
        for watcher in self._watchers:
            if not free:
                return
            for file_path in watcher.scan():
                slot = watcher.slot_of(file_path)
                if slot in free and watcher.begin(file_path):
                    self._start(watcher, file_path, slot)
                    free.remove(slot)
                    if not free:
                        return

    def _start(self, watcher: DirWatcher, file_path: Path, slot: Slot) -> None:
        try:
            located = LocatedFile.resolve(file_path)
            process = self._context.Process(
                target=run_job, args=(located, config.config.tools(), slot), name=f"baycone-{slot}"
            )
            process.start()
        except Exception:
            log.exception(f"Processing could not be started for '{file_path}'")
            watcher.finish(file_path, failed=True)
            return
        log.info(f"Processing '{file_path}' in the {slot} slot")
        self._jobs[slot] = RunningJob(process, watcher, file_path)

    def _collect_finished(self) -> None:
        for slot, job in list(self._jobs.items()):
            if job.process.exitcode is not None:
                del self._jobs[slot]
                self._settle(job, stopping=False)

    @staticmethod
    def _settle(job: RunningJob, stopping: bool) -> None:
        """Closes the attempt of a job that exited, by its `JobResult`."""
        watcher, file_path, code = job.watcher, job.file_path, job.process.exitcode
        match code:
            case JobResult.DONE | JobResult.STOPPED:
                watcher.finish(file_path, failed=False)
            case JobResult.FAILED:
                watcher.finish(file_path, failed=True)
            case JobResult.NO_SPACE:
                watcher.pause()
                watcher.finish(file_path, failed=False)
            case _:
                _kill_group(job.process.pid)
                if stopping:
                    # Killed by the stop itself, which is not the file's fault
                    watcher.finish(file_path, failed=False)
                else:
                    # E.g. the OOM killer; counted like a service that died, so a file that kills it is given up on
                    log.error(f"Processing of '{file_path}' died with exit code {code}, it will be tried again")
                    watcher.abandon(file_path)
        job.process.close()

    def _wait(self) -> None:
        """Until the poll interval is over, or sooner when a job ends and frees its slot."""
        sentinels = [job.process.sentinel for job in self._jobs.values()]
        if sentinels:
            wait(sentinels, timeout=config.config.poll_interval)
        else:
            time.sleep(config.config.poll_interval)

    def main_loop(self) -> None:
        while not self._stop_signal:
            try:
                self._reload_config()
                self._collect_finished()
                self._dispatch()
            except Exception:
                log.exception("Unexpected error, carrying on")
            self._wait()

    def start(self) -> int:
        log.info("Starting the service...")

        if missing_envs := config.Constants.get_missing_env_variables():
            log.error(f"Mandatory environment variables are not set: {", ".join(missing_envs)}")
            return 1

        self._config_file = FileChangeTracker(Path(config.Constants.FILE_CONFIG))
        # Primed before reading, so an edit made during the read is picked up by the next check
        self._config_file.changed()
        try:
            self._load_config()
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.error(f"Configuration '{config.Constants.FILE_CONFIG}' could not be loaded: {e}")
            return 1
        log.info("Service started")

        # PID 1 in a container ignores SIGTERM without a handler, so `docker stop` would wait for SIGKILL
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        try:
            self.main_loop()
        except KeyboardInterrupt:
            self.stop()
        return 0

    def stop(self) -> None:
        """Interrupts the jobs, which kill their ffmpeg and clean up; a stop never fails a file."""
        log.info("Stopping the service...")
        self._stop_signal = True

        for job in self._jobs.values():
            job.process.terminate()
        deadline = time.monotonic() + STOP_TIMEOUT
        for job in self._jobs.values():
            job.process.join(max(deadline - time.monotonic(), 0.0))
            if job.process.is_alive():
                log.warning(f"Processing of '{job.file_path}' did not stop in time, killing it")
                job.process.kill()
                job.process.join()

        for slot, job in list(self._jobs.items()):
            del self._jobs[slot]
            self._settle(job, stopping=True)

    @staticmethod
    def _handle_sigterm(signum: int, frame: FrameType | None) -> None:
        # A repeated SIGTERM must not interrupt the shutdown itself
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt


def main() -> int:
    setup_logging()
    return Service().start()
