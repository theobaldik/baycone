import os
from pathlib import Path

import pytest

from baycone.config import Constants
from baycone.watcher import DirWatcher


@pytest.fixture
def watcher(location_config) -> DirWatcher:
    return DirWatcher(location_config)


def settled(watcher: DirWatcher) -> Path | None:
    """A file is ready once it is unchanged since the previous scan."""
    watcher.get_new_file()
    return watcher.get_new_file()


def modify(file_path: Path) -> None:
    with file_path.open("ab") as f:
        f.write(b"x")
    stat = file_path.stat()
    os.utime(file_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


class TestInit:
    def test_special_dirs_are_created(self, watcher, location_dir):
        for special_dir in watcher.special_dirs:
            assert special_dir.is_dir()

    def test_watches_the_location_abspath(self, watcher, location_dir):
        assert watcher.special_dirs.root_dir == location_dir


class TestGetNewFile:
    def test_returns_none_when_directory_empty(self, watcher):
        assert settled(watcher) is None

    def test_returns_matching_file(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        assert settled(watcher) == video

    def test_skips_file_with_wrong_extension(self, watcher, location_dir):
        (location_dir / "document.txt").touch()
        assert settled(watcher) is None

    def test_returns_none_when_no_extensions_allowed(self, location_config, location_dir):
        location_config.allowed_extensions = []
        watcher = DirWatcher(location_config)
        (location_dir / "movie.mp4").touch()
        assert settled(watcher) is None

    def test_extension_match_is_case_insensitive(self, watcher, location_dir):
        video = location_dir / "movie.MP4"
        video.touch()
        assert settled(watcher) == video

    def test_finds_file_in_subdirectory(self, watcher, location_dir):
        subdir = location_dir / "ShowName" / "Season 1"
        subdir.mkdir(parents=True)
        video = subdir / "episode.mkv"
        video.touch()
        assert settled(watcher) == video

    def test_skips_files_inside_special_dirs(self, watcher):
        for special_dir in watcher.special_dirs:
            (special_dir / "inside.mp4").touch()
        assert settled(watcher) is None

    def test_does_not_descend_into_special_dirs(self, watcher, location_dir, monkeypatch):
        (watcher.special_dirs.processed_dir / "Show").mkdir()
        visited = []
        real_walk = Path.walk

        def recording_walk(self, *args, **kwargs):
            for entry in real_walk(self, *args, **kwargs):
                visited.append(entry[0])
                yield entry

        monkeypatch.setattr(Path, "walk", recording_walk)
        list(watcher._iter_files({".mp4"}))
        assert visited == [location_dir]


class TestStability:
    def test_new_file_is_not_returned_on_first_scan(self, watcher, location_dir):
        (location_dir / "movie.mp4").touch()
        assert watcher.get_new_file() is None

    def test_file_still_being_written_is_skipped(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        watcher.get_new_file()
        modify(video)
        assert watcher.get_new_file() is None
        assert watcher.get_new_file() == video

    def test_returns_first_stable_file(self, watcher, location_dir):
        growing = location_dir / "a-growing.mp4"
        stable = location_dir / "b-stable.mp4"
        growing.touch()
        stable.touch()
        watcher.get_new_file()
        modify(growing)
        assert watcher.get_new_file() == stable


class TestMarkFailed:
    def test_failed_file_is_skipped(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        assert settled(watcher) == video
        watcher.mark_failed(video)
        assert watcher.get_new_file() is None

    def test_failed_file_does_not_block_others(self, watcher, location_dir):
        failing = location_dir / "a-failing.mp4"
        other = location_dir / "b-other.mp4"
        failing.touch()
        other.touch()
        first = settled(watcher)
        watcher.mark_failed(first)
        assert watcher.get_new_file() == ({failing, other} - {first}).pop()

    def test_failed_file_is_retried_once_modified(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.mark_failed(video)
        modify(video)
        assert settled(watcher) == video


class TestConfigOverride:
    def test_override_is_applied(self, watcher, location_config, location_dir):
        override = location_dir / Constants.FILE_CONFIG_OVERRIDE
        override.write_text("target_video_bitrate: 777\n")
        watcher.get_new_file()
        assert location_config.target_video_bitrate == 777

    def test_removed_override_restores_location_config(self, watcher, location_config, location_dir):
        before = location_config.target_video_bitrate
        override = location_dir / Constants.FILE_CONFIG_OVERRIDE
        override.write_text("target_video_bitrate: 777\n")
        watcher.get_new_file()
        override.unlink()
        watcher.get_new_file()
        assert location_config.target_video_bitrate == before
