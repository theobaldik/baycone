import logging
import time
from pathlib import Path

import baycone.logger
from baycone import config
from baycone.config import LocationConfig, SpecialDirs

log = logging.getLogger(__name__)


class DirWatcher:
    """Watches a directory for new files to process."""

    def __init__(self, location_config: LocationConfig) -> None:
        self._dir_path: Path = location_config.abspath
        self._location_config = location_config
        self._locked_files: set = set()

        self.special_dirs: SpecialDirs = SpecialDirs(self._dir_path)
        self._create_special_dirs()

    def get_new_file(self) -> Path | None:
        """Returns a new file to be processed or `None` if no supported file is found."""
        config.load_config_override(self._location_config)
        if not self._location_config.allowed_extensions:
            return None
        for file_path in self._dir_path.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in self._location_config.allowed_extensions:
                continue
            for spec_dir in self.special_dirs:
                if file_path.is_relative_to(spec_dir):
                    break
            # File not in spec dir
            else:
                if self._is_file_locked(file_path):
                    if file_path not in self._locked_files:
                        log.info(f"New locked file found: '{file_path}'")
                        self._locked_files.add(file_path)
                    continue

                if file_path in self._locked_files:
                    self._locked_files.remove(file_path)
                    log.info(f"File unlocked: '{file_path}'")
                else:
                    log.info(f"New file found: '{file_path}'")

                return file_path
        return None

    @staticmethod
    def _is_file_locked(file_path: Path, interval: float = 1.0) -> bool:
        """Returns `True` if the file is still being written to, `False` otherwise."""
        try:
            size_before = file_path.stat().st_size
            time.sleep(interval)
            return size_before != file_path.stat().st_size
        except OSError:
            return True

    def _create_special_dirs(self) -> None:
        if not self.special_dirs.exists():
            logging.info(f"Creating special directories")
            self.special_dirs.create()
