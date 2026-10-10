import logging
import signal
import sys
import time
from pathlib import Path
from types import FrameType

from baycone import config
from baycone.encoding import Encoder
from baycone.logger import setup_logging
from baycone.watcher import DirWatcher

log = logging.getLogger(__name__)


class Service:
    def __init__(self) -> None:
        self._watchers: list[DirWatcher] = []
        self._encoder: Encoder = Encoder()
        self._stop_signal: bool = False

    def _init_watchers(self) -> None:
        for lc in config.config.locations:
            self._watchers.append(DirWatcher(lc))

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
            if not self._process_next():
                time.sleep(config.config.poll_interval)

    def start(self) -> int:
        log.info("Starting the service...")

        if missing_envs := config.Constants.get_missing_env_variables():
            log.error(f"Mandatory environment variables are not set: {", ".join(missing_envs)}")
            return 1

        config.load_config(Path(config.Constants.FILE_CONFIG))
        self._init_watchers()
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
