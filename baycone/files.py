import os
from contextlib import suppress
from pathlib import Path

type Signature = tuple[int, int]


def file_signature(file_path: Path) -> Signature | None:
    """Size and mtime, `None` for a missing or unreadable file."""
    try:
        stat = file_path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(directory: Path) -> None:
    """Makes a rename inside `directory` survive a power loss; not every network filesystem can sync a directory."""
    with suppress(OSError):
        fsync(directory)


def write_atomic(file_path: Path, text: str) -> None:
    """The file holds either its previous or the new content, also after a power loss."""
    temp_path = file_path.with_name(f"{file_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    temp_path.replace(file_path)
    fsync_dir(file_path.parent)
