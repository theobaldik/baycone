import logging
import time
from collections.abc import Iterator
from pathlib import Path

from baycone.encoding import Analyzer
from baycone.files import Signature, file_signature
from baycone_service import config
from baycone_service.config import Constants, LocationConfig, SpecialDirs
from baycone_service.layout import Slot
from baycone_service.state import Attempt, State

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
    """
    Finds the files of one location that are ready, and keeps their attempts in its `State`.

    Not thread-safe: only the service's dispatcher uses it, the jobs run in child processes.
    """

    def __init__(self, location_config: LocationConfig) -> None:
        self._dir_path: Path = location_config.abspath
        self._location_config: LocationConfig = location_config
        self._base_config: dict = location_config.to_dict()
        self._override_file: FileChangeTracker = FileChangeTracker(self._dir_path / Constants.FILE_CONFIG_OVERRIDE)

        # A file is ready once its signature is unchanged since the previous scan
        self._signatures: dict[Path, Signature] = {}
        self._paused_until: float = 0.0
        self._running: set[Path] = set()
        # The slot of a file version, valid until the location's config changes
        self._slots: dict[Path, tuple[Signature, Slot]] = {}

        self.special_dirs: SpecialDirs = SpecialDirs(self._dir_path)
        self._create_special_dirs()
        # Whatever a crash left behind; a new watcher never has a job running in its location
        self.special_dirs.clear_workdirs()
        self._state: State = State.load(self.special_dirs.state_file)

        # Loaded here rather than by the first scan, where it would count as a change and retry the failed files
        self._override_file.changed()
        config.load_config_override(self._location_config, self._base_config)

    @property
    def dir_path(self) -> Path:
        return self._dir_path

    def scan(self) -> list[Path]:
        """The files ready to be processed, alphabetically by their path; failed and running ones are left out."""
        self._reload_config_override()
        if time.monotonic() < self._paused_until:
            return []

        extensions = {ext.lower() for ext in self._location_config.allowed_extensions}
        if not extensions:
            return []

        signatures: dict[Path, Signature] = {}
        ready: list[Path] = []

        for file_path in self._iter_files(extensions):
            signature = file_signature(file_path)
            if signature is None:
                continue
            signatures[file_path] = signature

            if file_path not in self._signatures:
                log.info(f"New file found: '{file_path}'")
            if (
                self._signatures.get(file_path) != signature
                or self._is_failed(file_path, signature)
                or file_path in self._running
            ):
                continue
            ready.append(file_path)

        self._signatures = signatures
        self._forget_deleted()
        return sorted(ready, key=self._order)

    def slot_of(self, file_path: Path) -> Slot:
        """
        Probes the file, once per version and config: an invalid file takes the copy slot, where it fails fast.

        :param file_path: a file `scan` returned
        """
        signature = self._signatures.get(file_path)
        cached = self._slots.get(file_path)
        if cached and cached[0] == signature:
            return cached[1]

        info = Analyzer(self._location_config, config.config.tools()).analyze(file_path)
        slot = Slot.ENCODE if info.is_valid and info.video_stream.shall_encode else Slot.COPY
        self._slots[file_path] = (signature, slot)
        return slot

    def begin(self, file_path: Path) -> bool:
        """
        Opens an attempt; every `True` must be paired with `finish` or `abandon`.

        :return: `False` for a file the service kept dying on, which is failed instead of blocking the queue forever
        """
        attempts = self._state.in_progress
        key, signature = self._key(file_path), self._signatures[file_path]

        attempt = attempts.get(key)
        if attempt and attempt.signature == signature:
            attempt.attempts += 1
        else:
            attempt = attempts[key] = Attempt(signature)

        if attempt.attempts > MAX_ATTEMPTS:
            log.error(f"The service was cut off {MAX_ATTEMPTS} times in a row while processing '{file_path}'")
            self._close(file_path, failed=True)
            return False

        self._running.add(file_path)
        self._save_state()
        return True

    def finish(self, file_path: Path, failed: bool) -> None:
        """Ends the attempt started by `begin`; a failed file is skipped until it is modified."""
        self._running.discard(file_path)
        self._close(file_path, failed)

    def abandon(self, file_path: Path) -> None:
        """Leaves the attempt open, as a service that died would, so the file is failed after `MAX_ATTEMPTS`."""
        self._running.discard(file_path)

    def update_config(self, location_config: LocationConfig) -> None:
        """
        Takes over a reloaded config of the same location. Nothing else is reset: a job may be running in it.

        :param location_config: the location's new config, its override is applied to it
        """
        self._location_config = location_config
        self._base_config = location_config.to_dict()
        config.load_config_override(self._location_config, self._base_config)
        self._paused_until = 0.0
        self._slots = {}

    def retry_failed(self) -> None:
        """Whatever made the files fail may have been fixed by now."""
        if self._state.failed:
            log.info(f"Retrying {len(self._state.failed)} failed file(s) in '{self._dir_path}'")
            self._state.failed = {}
            self._save_state()

    def pause(self) -> None:
        log.warning(f"Location '{self._dir_path}' is paused for {PAUSE_SECONDS / 60:.0f} min")
        self._paused_until = time.monotonic() + PAUSE_SECONDS

    def _close(self, file_path: Path, failed: bool) -> None:
        attempt = self._state.in_progress.pop(self._key(file_path), None)
        if failed and attempt:
            self._state.failed[self._key(file_path)] = attempt.signature
            log.warning(f"File '{file_path}' will be retried once it changes or the configuration is reloaded")
        self._save_state()

    def _order(self, file_path: Path) -> tuple:
        # A folder at a time, so `Show/` lists before `Show 2/` (as strings, ' ' sorts before '/'); case as people sort
        parts = file_path.relative_to(self._dir_path).parts
        return tuple(part.casefold() for part in parts), parts

    def _key(self, file_path: Path) -> str:
        return file_path.relative_to(self._dir_path).as_posix()

    def _is_failed(self, file_path: Path, signature: Signature) -> bool:
        return self._state.failed.get(self._key(file_path)) == signature

    def _forget_deleted(self) -> None:
        """Failures and open attempts of files that are gone."""
        self._slots = {path: slot for path, slot in self._slots.items() if path in self._signatures}
        # An empty scan is more likely a library that is not mounted right now than one that was emptied
        if not self._signatures:
            return
        state = self._state
        failed = {key: sig for key, sig in state.failed.items() if self._dir_path / key in self._signatures}
        in_progress = {key: a for key, a in state.in_progress.items() if self._dir_path / key in self._signatures}
        if failed != state.failed or in_progress != state.in_progress:
            state.failed, state.in_progress = failed, in_progress
            self._save_state()

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
            self._slots = {}
            self.retry_failed()

    def _create_special_dirs(self) -> None:
        if not self.special_dirs.exists():
            log.info(f"Creating special directories in '{self._dir_path}'")
            self.special_dirs.create()
