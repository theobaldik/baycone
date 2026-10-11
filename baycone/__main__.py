import logging
import signal
import sys
import time
from pathlib import Path
from types import FrameType

import yaml

from baycone import config
from baycone.encoding import Encoder
from baycone.logger import setup_logging
from baycone.watcher import DirWatcher, FileChangeTracker

log = logging.getLogger(__name__)


class Service:
    def __init__(self) -> None:
        self._watchers: list[DirWatcher] = []
        self._encoder: Encoder = Encoder()
        self._stop_signal: bool = False
        self._config_file: FileChangeTracker | None = None

    def _load_config(self) -> None:
        config.load_config(self._config_file.file_path)
        self._watchers = [DirWatcher(lc) for lc in config.config.locations]

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

    def _process_next(self) -> bool:
        """Returns `True` if a file was picked up."""
        processed = False
        for watcher in self._watchers:
            if file := watcher.get_new_file():
                processed = True
                try:
                    succeeded = self._encoder.process(file)
                except Exception:
                    log.exception(f"Processing failed for '{file}'")
                    succeeded = False
                if not succeeded:
                    watcher.mark_failed(file)
        return processed

    def main_loop(self) -> None:
        while not self._stop_signal:
            self._reload_config()
            if not self._process_next():
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
        except (OSError, yaml.YAMLError) as e:
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


if __name__ == "__main__":
    setup_logging()
    sys.exit(Service().start())
