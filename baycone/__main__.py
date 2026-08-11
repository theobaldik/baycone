import time
import logging
from pathlib import Path

import baycone.logger
from baycone import config
from baycone.watcher import DirWatcher
from baycone.encoding import Encoder

logging.getLogger(__name__)


class Service:
    def __init__(self):
        self._watchers: list[DirWatcher] = []
        self._encoder: Encoder = Encoder()
        self._stop_signal: bool = False

    def _init_watchers(self):
        for lc in config.config.locations:
            self._watchers.append(DirWatcher(lc))

    def main_loop(self):
        while not self._stop_signal:
            for watcher in self._watchers:
                if file := watcher.get_new_file():
                    self._encoder.process(file)
            time.sleep(config.config.poll_interval)

    def start(self):
        logging.info("Starting the service...")

        if missing_envs := config.Constants.get_missing_env_variables():
            logging.error(f"Mandatory environment variables are not set: {", ".join(missing_envs)}")
            self.stop()
            return

        logging.info("Service started")
        config.load_config(Path(config.Constants.FILE_CONFIG))
        self._init_watchers()
        try:
            self.main_loop()
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        logging.info("Stopping the service...")
        self._stop_signal = True


if __name__ == "__main__":
    service = Service()
    service.start()
