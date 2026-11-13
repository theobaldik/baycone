import logging
import signal
import time
from dataclasses import fields
from pathlib import Path
from types import FrameType

import yaml

from baycone.encoding import Encoder
from baycone.errors import BayconeError, InsufficientSpaceError
from baycone_service import config
from baycone_service.layout import LocatedFile, archive_original
from baycone_service.logger import setup_logging
from baycone_service.watcher import DirWatcher, FileChangeTracker

log = logging.getLogger(__name__)


class Service:
    def __init__(self) -> None:
        self._watchers: list[DirWatcher] = []
        self._stop_signal: bool = False
        self._config_file: FileChangeTracker | None = None

    def _load_config(self) -> None:
        """All or nothing: a location whose watcher cannot be set up leaves the previous config in place."""
        previous = {f.name: getattr(config.config, f.name) for f in fields(config.config)}
        try:
            config.load_config(self._config_file.file_path)
            self._watchers = [DirWatcher(lc) for lc in config.config.locations]
        except Exception:
            for name, value in previous.items():
                setattr(config.config, name, value)
            raise

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

    def _process_file(self, file_path: Path) -> bool:
        """`False` for a file that failed. The original is archived only once its output is published."""
        located = LocatedFile.resolve(file_path)
        encoder = Encoder(located.location_config, config.config.tools())
        try:
            encoder.process(located.job())
        except InsufficientSpaceError:
            raise
        except BayconeError as e:
            log.error(f"Processing aborted: {e}")
            return False
        archive_original(located)
        return True

    def _process_next(self) -> bool:
        """Returns `True` if a file was picked up."""
        processed = False
        for watcher in self._watchers:
            if file := watcher.get_new_file():
                processed = True
                failed = False
                try:
                    failed = not self._process_file(file)
                except InsufficientSpaceError as e:
                    # Not the file's fault: it is left alone and tried again later
                    log.warning(f"Not enough free space: {e}")
                    watcher.pause()
                except Exception:
                    log.exception(f"Processing failed for '{file}'")
                    failed = True
                finally:
                    # Reached on a stop signal too, so only a service that died leaves the attempt open
                    watcher.finish(file, failed)
        return processed

    def main_loop(self) -> None:
        while not self._stop_signal:
            try:
                self._reload_config()
                idle = not self._process_next()
            except Exception:
                log.exception("Unexpected error, carrying on")
                idle = True
            if idle:
                time.sleep(config.config.poll_interval)

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
        log.info("Stopping the service...")
        self._stop_signal = True

    @staticmethod
    def _handle_sigterm(signum: int, frame: FrameType | None) -> None:
        # A repeated SIGTERM must not interrupt the shutdown itself
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt


def main() -> int:
    setup_logging()
    return Service().start()
