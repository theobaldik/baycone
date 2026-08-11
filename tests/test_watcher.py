"""
Tests for baycone/watcher.py

`DirWatcher` takes a `LocationConfig` and resolves its watched directory through `LocationConfig.abspath`,
so the `location_config` fixture (see conftest.py) roots everything in `tmp_path`.
Lock detection is size-comparison based; `_is_file_locked` sleeps, so `get_new_file` tests patch it out.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from baycone.watcher import DirWatcher


@pytest.fixture
def watcher(location_config) -> DirWatcher:
    return DirWatcher(location_config)


def unlocked(watcher: DirWatcher):
    """Patches lock detection so every file counts as ready to process."""
    return patch.object(watcher, "_is_file_locked", return_value=False)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_special_dirs_are_created(self, watcher, location_dir):
        for special_dir in watcher.special_dirs:
            assert special_dir.is_dir()

    def test_watches_the_location_abspath(self, watcher, location_dir):
        assert watcher.special_dirs.root_dir == location_dir


# ---------------------------------------------------------------------------
# get_new_file - file discovery
# ---------------------------------------------------------------------------


class TestGetNewFile:
    def test_returns_none_when_directory_empty(self, watcher):
        with unlocked(watcher):
            assert watcher.get_new_file() is None

    def test_returns_matching_file(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        with unlocked(watcher):
            assert watcher.get_new_file() == video

    def test_skips_file_with_wrong_extension(self, watcher, location_dir):
        (location_dir / "document.txt").touch()
        with unlocked(watcher):
            assert watcher.get_new_file() is None

    def test_returns_none_when_no_extensions_allowed(self, location_config, location_dir):
        location_config.allowed_extensions = []
        watcher = DirWatcher(location_config)
        (location_dir / "movie.mp4").touch()
        with unlocked(watcher):
            assert watcher.get_new_file() is None

    def test_extension_match_is_case_insensitive(self, watcher, location_dir):
        video = location_dir / "movie.MP4"
        video.touch()
        with unlocked(watcher):
            assert watcher.get_new_file() == video

    def test_finds_file_in_subdirectory(self, watcher, location_dir):
        subdir = location_dir / "ShowName" / "Season 1"
        subdir.mkdir(parents=True)
        video = subdir / "episode.mkv"
        video.touch()
        with unlocked(watcher):
            assert watcher.get_new_file() == video

    def test_skips_files_inside_special_dirs(self, watcher):
        for special_dir in watcher.special_dirs:
            (special_dir / "inside.mp4").touch()
        with unlocked(watcher):
            assert watcher.get_new_file() is None

    def test_skips_locked_file(self, watcher, location_dir):
        (location_dir / "movie.mp4").touch()
        with patch.object(watcher, "_is_file_locked", return_value=True):
            assert watcher.get_new_file() is None

    def test_returns_first_unlocked_file(self, watcher, location_dir):
        (location_dir / "locked.mp4").touch()
        unlocked_file = location_dir / "unlocked.mp4"
        unlocked_file.touch()

        with patch.object(watcher, "_is_file_locked", side_effect=lambda p: p.name == "locked.mp4"):
            assert watcher.get_new_file() == unlocked_file

    def test_locked_file_is_returned_once_unlocked(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()

        with patch.object(watcher, "_is_file_locked", return_value=True):
            assert watcher.get_new_file() is None
        with unlocked(watcher):
            assert watcher.get_new_file() == video


# ---------------------------------------------------------------------------
# _is_file_locked - size comparison
# ---------------------------------------------------------------------------


class TestIsFileLocked:
    def test_not_locked_when_size_is_stable(self, tmp_path):
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"data")
        assert DirWatcher._is_file_locked(video, interval=0) is False

    def test_locked_when_size_changes(self, tmp_path):
        video = tmp_path / "movie.mp4"
        video.write_bytes(b"data")

        real_stat = Path.stat
        sizes = iter([10, 20])

        class FakeStat:
            def __init__(self, size):
                self.st_size = size

        with patch.object(Path, "stat", lambda self, **kw: FakeStat(next(sizes))):
            assert DirWatcher._is_file_locked(video, interval=0) is True

        assert Path.stat is real_stat

    def test_locked_when_file_disappears(self, tmp_path):
        assert DirWatcher._is_file_locked(tmp_path / "missing.mp4", interval=0) is True
