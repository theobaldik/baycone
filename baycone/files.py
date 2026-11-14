"""Change detection and durable writes; anything that must survive a power loss goes through these."""

import os
from contextlib import suppress
from pathlib import Path

# (size, mtime in ns), the one notion of "this file changed"
type Signature = tuple[int, int]


def file_signature(file_path: Path) -> Signature | None:
    """
    Identifies a file's content by its size and mtime.

    :param file_path: the file
    :return: the signature, `None` for a missing or unreadable file
    """
    try:
        stat = file_path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def fsync(path: Path) -> None:
    """
    Flushes a file or directory to disk.

    :param path: the file or directory
    """
    # Windows only flushes a handle opened for writing
    fd = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(directory: Path) -> None:
    """
    Makes a rename survive a power loss. Errors are ignored: not every network filesystem can sync a directory.

    :param directory: the directory holding the renamed file
    """
    with suppress(OSError):
        fsync(directory)


def write_atomic(file_path: Path, text: str) -> None:
    """
    Replaces a file's content; it holds either the previous or the new one, also after a power loss.

    :param file_path: the file to write
    :param text: the content, written as UTF-8
    """
    temp_path = file_path.with_name(f"{file_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    temp_path.replace(file_path)
    fsync_dir(file_path.parent)
