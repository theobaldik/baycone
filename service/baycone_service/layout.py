import logging
from dataclasses import dataclass
from pathlib import Path

from baycone.encoding import Job
from baycone.files import fsync_dir
from baycone_service.config import LocationConfig, SpecialDirs, config

log = logging.getLogger(__name__)


@dataclass
class LocatedFile:
    """A media file inside a watched location, and where its outputs go."""

    file_path: Path
    location_config: LocationConfig
    special_dirs: SpecialDirs

    @classmethod
    def resolve(cls, file_path: Path) -> LocatedFile:
        for lc in config.locations:
            if file_path.is_relative_to(lc.abspath):
                return cls(file_path, lc, SpecialDirs(lc.abspath))
        raise ValueError(f"No 'LocationConfig' found for '{file_path}'")

    @property
    def relative_path(self) -> Path:
        return self.file_path.relative_to(self.special_dirs.root_dir)

    @property
    def original_path(self) -> Path:
        return self.special_dirs.originals_dir / self.relative_path

    @property
    def processed_path(self) -> Path:
        return self.special_dirs.processed_dir / self.relative_path.with_suffix(self.location_config.target_extension)

    def job(self) -> Job:
        return Job(
            input_file=self.file_path,
            output_file=self.processed_path,
            work_dir=self.special_dirs.temp_dir,
            name=self.relative_path.as_posix(),
        )


def _free_path(path: Path) -> Path:
    """`path`, or `<stem>.<n><suffix>` when it is taken."""
    candidate, number = path, 0
    while candidate.exists():
        number += 1
        candidate = path.with_stem(f"{path.stem}.{number}")
    return candidate


def _remove_empty_dirs(directory: Path, root_dir: Path) -> None:
    """Stops at `root_dir` (exclusive) or the first non-empty directory."""
    while directory != root_dir and directory.is_relative_to(root_dir):
        try:
            # Fails on non-empty directories, so unprocessed files are never removed
            directory.rmdir()
        except OSError:
            return
        log.info(f"Removed empty directory '{directory}'")
        directory = directory.parent


def archive_original(located: LocatedFile) -> None:
    source_path = located.file_path
    # An earlier original of the same name is kept, a rename would silently replace it
    target_path = _free_path(located.original_path)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.rename(target_path)
    fsync_dir(target_path.parent)
    fsync_dir(source_path.parent)
    log.info(f"File '{source_path}' moved to '{target_path}'")

    _remove_empty_dirs(source_path.parent, located.special_dirs.root_dir)
