import logging
from collections.abc import Iterator
from pathlib import Path

from baycone import config
from baycone.config import Constants, LocationConfig, SpecialDirs

log = logging.getLogger(__name__)

type Signature = tuple[int, int]


class FileChangeTracker:
    """A missing file counts as mtime `None`, so its deletion is a change too."""

    def __init__(self, file_path: Path) -> None:
        self.file_path: Path = file_path
        self._mtime: int | None = None

    def changed(self) -> bool:
        """Whether the mtime differs from the previous call."""
        try:
            mtime = self.file_path.stat().st_mtime_ns
        except OSError:
            mtime = None

        if mtime == self._mtime:
            return False
        self._mtime = mtime
        return True


class DirWatcher:

    def __init__(self, location_config: LocationConfig) -> None:
        self._dir_path: Path = location_config.abspath
        self._location_config: LocationConfig = location_config
        self._base_config: dict = location_config.to_dict()
        self._override_file: FileChangeTracker = FileChangeTracker(self._dir_path / Constants.FILE_CONFIG_OVERRIDE)

        # A file is ready once its signature is unchanged since the previous scan
        self._signatures: dict[Path, Signature] = {}
        self._failed: dict[Path, Signature] = {}

        self.special_dirs: SpecialDirs = SpecialDirs(self._dir_path)
        self._create_special_dirs()

    def get_new_file(self) -> Path | None:
        self._reload_config_override()

        extensions = {ext.lower() for ext in self._location_config.allowed_extensions}
        if not extensions:
            return None

        signatures: dict[Path, Signature] = {}
        ready: Path | None = None

        for file_path in self._iter_files(extensions):
            try:
                stat = file_path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            signatures[file_path] = signature

            if file_path not in self._signatures:
                log.info(f"New file found: '{file_path}'")
            if ready or self._signatures.get(file_path) != signature or self._failed.get(file_path) == signature:
                continue
            ready = file_path

        self._signatures = signatures
        self._failed = {path: sig for path, sig in self._failed.items() if path in signatures}
        return ready

    def mark_failed(self, file_path: Path) -> None:
        """Skips `file_path` until it is modified."""
        if signature := self._signatures.get(file_path):
            self._failed[file_path] = signature
            log.warning(f"File '{file_path}' will be retried once it changes")

    def _iter_files(self, extensions: set[str]) -> Iterator[Path]:
        special_dirs = set(self.special_dirs)
        for dir_path, dir_names, file_names in self._dir_path.walk():
            dir_names[:] = [name for name in dir_names if dir_path / name not in special_dirs]
            for name in file_names:
                file_path = dir_path / name
                if file_path.suffix.lower() in extensions:
                    yield file_path

    def _reload_config_override(self) -> None:
        if self._override_file.changed():
            config.load_config_override(self._location_config, self._base_config)

    def _create_special_dirs(self) -> None:
        if not self.special_dirs.exists():
            log.info(f"Creating special directories in '{self._dir_path}'")
            self.special_dirs.create()
