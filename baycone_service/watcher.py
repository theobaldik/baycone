import logging
import time
from collections.abc import Iterator
from pathlib import Path

from baycone.files import Signature, file_signature
from baycone_service import config
from baycone_service.config import Constants, LocationConfig, SpecialDirs
from baycone_service.state import State

log = logging.getLogger(__name__)

# Pruned at any depth, so a location nested in another one is not scanned twice over
SPECIAL_DIR_NAMES: frozenset[str] = frozenset(
    directory.name for directory in (Constants.DIR_TEMP, Constants.DIR_ORIGINALS, Constants.DIR_PROCESSED)
)

PAUSE_SECONDS: float = 300.0

# Attempts on one file that ended with the service dying, before the file is given up on
MAX_ATTEMPTS: int = 3


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
        self._paused_until: float = 0.0

        self.special_dirs: SpecialDirs = SpecialDirs(self._dir_path)
        self._create_special_dirs()
        # Whatever a crash left behind; never called while a file is being processed
        self.special_dirs.clear_workdirs()
        self._state: State = State.load(self.special_dirs.state_file)

        # Loaded here rather than by the first scan, where it would count as a change and retry the failed files
        self._override_file.changed()
        config.load_config_override(self._location_config, self._base_config)

    def get_new_file(self) -> Path | None:
        """The returned file counts as being processed until `finish` is called."""
        self._reload_config_override()
        if time.monotonic() < self._paused_until:
            return None

        extensions = {ext.lower() for ext in self._location_config.allowed_extensions}
        if not extensions:
            return None

        signatures: dict[Path, Signature] = {}
        ready: Path | None = None

        for file_path in self._iter_files(extensions):
            signature = file_signature(file_path)
            if signature is None:
                continue
            signatures[file_path] = signature

            if file_path not in self._signatures:
                log.info(f"New file found: '{file_path}'")
            if ready or self._signatures.get(file_path) != signature or self._is_failed(file_path, signature):
                continue
            ready = file_path

        self._signatures = signatures
        self._forget_deleted_failures()

        if ready and not self._begin(ready):
            return None
        return ready

    def finish(self, file_path: Path, failed: bool) -> None:
        """Ends the attempt started by `get_new_file`; a failed file is skipped until it is modified."""
        self._state.current, self._state.current_signature, self._state.attempts = "", None, 0

        signature = self._signatures.get(file_path)
        if failed and signature:
            self._state.failed[self._key(file_path)] = signature
            log.warning(f"File '{file_path}' will be retried once it changes or the configuration is reloaded")
        self._save_state()

    def retry_failed(self) -> None:
        """Whatever made the files fail may have been fixed by now."""
        if self._state.failed:
            log.info(f"Retrying {len(self._state.failed)} failed file(s) in '{self._dir_path}'")
            self._state.failed = {}
            self._save_state()

    def pause(self) -> None:
        log.warning(f"Location '{self._dir_path}' is paused for {PAUSE_SECONDS / 60:.0f} min")
        self._paused_until = time.monotonic() + PAUSE_SECONDS

    def _key(self, file_path: Path) -> str:
        return file_path.relative_to(self._dir_path).as_posix()

    def _is_failed(self, file_path: Path, signature: Signature) -> bool:
        return self._state.failed.get(self._key(file_path)) == signature

    def _forget_deleted_failures(self) -> None:
        # An empty scan is more likely a library that is not mounted right now than one that was emptied
        if not self._signatures:
            return
        failed = {key: sig for key, sig in self._state.failed.items() if self._dir_path / key in self._signatures}
        if failed != self._state.failed:
            self._state.failed = failed
            self._save_state()

    def _begin(self, file_path: Path) -> bool:
        """`False` for a file that the service keeps dying on, which would otherwise block the queue forever."""
        state = self._state
        key, signature = self._key(file_path), self._signatures[file_path]

        if (state.current, state.current_signature) == (key, signature):
            state.attempts += 1
        else:
            state.current, state.current_signature, state.attempts = key, signature, 1

        if state.attempts > MAX_ATTEMPTS:
            log.error(f"The service was cut off {MAX_ATTEMPTS} times in a row while processing '{file_path}'")
            self.finish(file_path, failed=True)
            return False

        self._save_state()
        return True

    def _save_state(self) -> None:
        self._state.save(self.special_dirs.state_file)

    def _iter_files(self, extensions: set[str]) -> Iterator[Path]:
        for dir_path, dir_names, file_names in self._dir_path.walk():
            dir_names[:] = [name for name in dir_names if name not in SPECIAL_DIR_NAMES]
            for name in file_names:
                file_path = dir_path / name
                if file_path.suffix.lower() in extensions:
                    yield file_path

    def _reload_config_override(self) -> None:
        if self._override_file.changed():
            config.load_config_override(self._location_config, self._base_config)
            self._paused_until = 0.0
            self.retry_failed()

    def _create_special_dirs(self) -> None:
        if not self.special_dirs.exists():
            log.info(f"Creating special directories in '{self._dir_path}'")
            self.special_dirs.create()
