import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone.encoding import FileInfo, VideoStream
from baycone_service.config import Constants, LocationConfig
from baycone_service.layout import Slot
from baycone_service.watcher import MAX_ATTEMPTS, PAUSE_SECONDS, DirWatcher, FileChangeTracker


@pytest.fixture
def watcher(location_config) -> DirWatcher:
    return DirWatcher(location_config)


def next_file(watcher: DirWatcher) -> Path | None:
    """What the service starts next: the first ready file whose attempt begins, left running."""
    for file_path in watcher.scan():
        if watcher.begin(file_path):
            return file_path
    return None


def settled(watcher: DirWatcher) -> Path | None:
    """A file is ready once it is unchanged since the previous scan."""
    watcher.scan()
    return next_file(watcher)


def modify(file_path: Path) -> None:
    with file_path.open("ab") as f:
        f.write(b"x")
    stat = file_path.stat()
    os.utime(file_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


class TestFileChangeTracker:
    def test_existing_file_is_a_change_on_first_call(self, tmp_path):
        file_path = tmp_path / "config.yml"
        file_path.write_text("a")
        assert FileChangeTracker(file_path).changed() is True

    def test_missing_file_is_no_change_on_first_call(self, tmp_path):
        assert FileChangeTracker(tmp_path / "config.yml").changed() is False

    def test_unchanged_file(self, tmp_path):
        file_path = tmp_path / "config.yml"
        file_path.write_text("a")
        tracker = FileChangeTracker(file_path)
        tracker.changed()
        assert tracker.changed() is False

    def test_modified_file(self, tmp_path):
        file_path = tmp_path / "config.yml"
        file_path.write_text("a")
        tracker = FileChangeTracker(file_path)
        tracker.changed()
        modify(file_path)
        assert tracker.changed() is True

    def test_deleted_file(self, tmp_path):
        file_path = tmp_path / "config.yml"
        file_path.write_text("a")
        tracker = FileChangeTracker(file_path)
        tracker.changed()
        file_path.unlink()
        assert tracker.changed() is True


class TestInit:
    def test_special_dirs_are_created(self, watcher, location_dir):
        for special_dir in watcher.special_dirs:
            assert special_dir.is_dir()

    def test_watches_the_location_abspath(self, watcher, location_dir):
        assert watcher.special_dirs.root_dir == location_dir


class TestScan:
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

    def test_skips_special_dirs_of_a_nested_location(self, watcher, location_dir):
        """Otherwise the outer location would process the inner one's originals and outputs all over again."""
        nested = location_dir / "kids"
        for name in (Constants.DIR_ORIGINALS, Constants.DIR_PROCESSED, Constants.DIR_ENCODED):
            (nested / name).mkdir(parents=True)
            (nested / name / "inside.mp4").touch()
        (nested / "pending.mp4").touch()
        assert settled(watcher) == nested / "pending.mp4"


class TestOrder:
    def test_ready_files_come_alphabetically(self, watcher, location_dir):
        names = ["b.mp4", "Show/S01E02.mkv", "a.mp4", "C.mp4", "Show/S01E01.mkv", "show.mp4"]
        for name in names:
            (location_dir / name).parent.mkdir(exist_ok=True)
            (location_dir / name).touch()
        watcher.scan()
        assert [watcher._key(path) for path in watcher.scan()] == [
            "a.mp4",
            "b.mp4",
            "C.mp4",
            "Show/S01E01.mkv",
            "Show/S01E02.mkv",
            "show.mp4",
        ]

    @pytest.mark.parametrize(
        "names",
        [
            ["dir-a/file-2.mkv", "dir-b/file-1.mkv"],
            ["Show/e02.mkv", "Show 2/e01.mkv"],
            ["dir/z.mkv", "dir-a/a.mkv"],
        ],
        ids=["folders first", "folder before a longer name", "a folder is listed as a whole"],
    )
    def test_whole_path_decides_a_folder_at_a_time(self, watcher, location_dir, names):
        for name in reversed(names):
            (location_dir / name).parent.mkdir(exist_ok=True)
            (location_dir / name).touch()
        watcher.scan()
        assert [watcher._key(path) for path in watcher.scan()] == names

    def test_files_are_started_alphabetically(self, watcher, location_dir):
        for name in ["c.mp4", "a.mp4", "b.mp4"]:
            (location_dir / name).touch()
        started = [settled(watcher), next_file(watcher), next_file(watcher)]
        assert [path.name for path in started] == ["a.mp4", "b.mp4", "c.mp4"]


class TestStability:
    def test_new_file_is_not_returned_on_first_scan(self, watcher, location_dir):
        (location_dir / "movie.mp4").touch()
        assert next_file(watcher) is None

    def test_file_still_being_written_is_skipped(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        next_file(watcher)
        modify(video)
        assert next_file(watcher) is None
        assert next_file(watcher) == video

    def test_returns_first_stable_file(self, watcher, location_dir):
        growing = location_dir / "a-growing.mp4"
        stable = location_dir / "b-stable.mp4"
        growing.touch()
        stable.touch()
        next_file(watcher)
        modify(growing)
        assert next_file(watcher) == stable


class TestFinish:
    def test_failed_file_is_skipped(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        assert settled(watcher) == video
        watcher.finish(video, failed=True)
        assert next_file(watcher) is None

    def test_finished_file_is_not_remembered_as_failed(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=False)
        assert next_file(watcher) == video

    def test_failed_file_is_still_skipped_after_a_restart(self, watcher, location_config, location_dir):
        """It may have taken hours to fail, and would again."""
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        assert settled(DirWatcher(location_config)) is None

    def test_failed_file_in_a_subdirectory_is_skipped_after_a_restart(self, watcher, location_config, location_dir):
        video = location_dir / "Show" / "Season 1" / "episode.mp4"
        video.parent.mkdir(parents=True)
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        assert settled(DirWatcher(location_config)) is None

    def test_retry_failed_brings_the_files_back(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        watcher.retry_failed()
        assert next_file(watcher) == video

    def test_deleted_failed_file_is_forgotten(self, watcher, location_dir):
        video, other = location_dir / "movie.mp4", location_dir / "other.mp4"
        video.touch()
        other.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        video.unlink()
        next_file(watcher)
        assert "movie.mp4" not in watcher._state.failed

    def test_failed_files_survive_a_library_that_is_not_mounted(self, watcher, location_dir):
        """An empty scan must not make the service forget, and later redo, every failure."""
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        hidden = video.rename(location_dir.parent / "movie.mp4")
        next_file(watcher)
        hidden.rename(video)
        assert settled(watcher) is None

    def test_failed_file_does_not_block_others(self, watcher, location_dir):
        failing = location_dir / "a-failing.mp4"
        other = location_dir / "b-other.mp4"
        failing.touch()
        other.touch()
        first = settled(watcher)
        watcher.finish(first, failed=True)
        assert next_file(watcher) == ({failing, other} - {first}).pop()

    def test_failed_file_is_retried_once_modified(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        modify(video)
        assert settled(watcher) == video


class TestAttempts:
    """A file the service dies on is picked up again after every restart, and would block the queue forever."""

    def _die_while_processing(self, location_config, video: Path) -> None:
        """`get_new_file` without the `finish` a service that survived would call."""
        assert settled(DirWatcher(location_config)) == video

    def test_file_is_given_up_after_the_service_died_on_it_repeatedly(self, location_config, location_dir, caplog):
        video = location_dir / "movie.mp4"
        video.touch()
        for _ in range(MAX_ATTEMPTS):
            self._die_while_processing(location_config, video)
        assert settled(DirWatcher(location_config)) is None
        assert "cut off" in caplog.text

    def test_other_files_are_processed_once_it_is_given_up(self, location_config, location_dir):
        video = location_dir / "a-movie.mp4"
        video.touch()
        for _ in range(MAX_ATTEMPTS):
            self._die_while_processing(location_config, video)
        other = location_dir / "b-other.mp4"
        other.touch()
        assert settled(DirWatcher(location_config)) == other

    def test_finished_attempt_is_not_counted(self, location_config, location_dir):
        """A clean stop or an ordinary failure is no sign of a file that kills the service."""
        video = location_dir / "movie.mp4"
        video.touch()
        for _ in range(MAX_ATTEMPTS + 2):
            watcher = DirWatcher(location_config)
            assert settled(watcher) == video
            watcher.finish(video, failed=False)

    def test_changed_file_starts_counting_anew(self, location_config, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        for _ in range(MAX_ATTEMPTS):
            self._die_while_processing(location_config, video)
        modify(video)
        assert settled(DirWatcher(location_config)) == video


class TestPause:
    def test_paused_location_returns_no_files(self, watcher, location_dir):
        (location_dir / "movie.mp4").touch()
        watcher.scan()
        watcher.pause()
        assert next_file(watcher) is None

    def test_resumes_after_the_pause(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        watcher.scan()
        with patch("baycone_service.watcher.time.monotonic", return_value=1000.0):
            watcher.pause()
        with patch("baycone_service.watcher.time.monotonic", return_value=1000.0 + PAUSE_SECONDS):
            assert next_file(watcher) == video

    def test_changed_override_ends_the_pause(self, watcher, location_dir):
        """E.g. `check_free_space: false` for a location that reports a wrong free space."""
        video = location_dir / "movie.mp4"
        video.touch()
        watcher.scan()
        watcher.pause()
        (location_dir / Constants.FILE_CONFIG_OVERRIDE).write_text("check_free_space: false\n")
        assert next_file(watcher) == video


class TestStartup:
    def test_leftover_work_files_are_removed(self, location_config, location_dir):
        watcher = DirWatcher(location_config)
        encoded = watcher.special_dirs.encoded_dir / "movie.mp4"
        optimized = watcher.special_dirs.optimized_dir / "movie.mp4"
        encoded.touch()
        optimized.touch()

        DirWatcher(location_config)

        assert not encoded.exists()
        assert not optimized.exists()

    def test_first_pass_and_override_are_kept(self, location_config, location_dir):
        watcher = DirWatcher(location_config)
        passlog = watcher.special_dirs.passlog_dir / "pass1.json"
        override = location_dir / Constants.FILE_CONFIG_OVERRIDE
        passlog.touch()
        override.write_text("target_video_bitrate: 777\n")

        DirWatcher(location_config)

        assert passlog.is_file()
        assert override.is_file()

    def test_unreadable_state_starts_empty(self, location_config, location_dir):
        watcher = DirWatcher(location_config)
        watcher.special_dirs.state_file.write_text("{ not json")
        video = location_dir / "movie.mp4"
        video.touch()
        assert settled(DirWatcher(location_config)) == video

    def test_state_is_kept_in_the_temp_dir(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        state = json.loads((location_dir / Constants.FILE_STATE).read_text())
        assert list(state["failed"]) == ["movie.mp4"]


class TestConfigOverride:
    def test_override_is_applied(self, watcher, location_config, location_dir):
        override = location_dir / Constants.FILE_CONFIG_OVERRIDE
        override.write_text("target_video_bitrate: 777\n")
        next_file(watcher)
        assert location_config.target_video_bitrate == 777

    def test_override_present_at_startup_is_applied(self, location_config, location_dir):
        (location_dir / Constants.DIR_TEMP).mkdir()
        (location_dir / Constants.FILE_CONFIG_OVERRIDE).write_text("target_video_bitrate: 777\n")
        DirWatcher(location_config)
        assert location_config.target_video_bitrate == 777

    def test_override_present_at_startup_does_not_retry_failed_files(self, watcher, location_config, location_dir):
        """Only a change is a hint that the cause of the failures was fixed."""
        (location_dir / Constants.FILE_CONFIG_OVERRIDE).write_text("target_video_bitrate: 777\n")
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        assert settled(DirWatcher(location_config)) is None

    def test_changed_override_retries_failed_files(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        watcher.finish(video, failed=True)
        (location_dir / Constants.FILE_CONFIG_OVERRIDE).write_text("target_video_codec: hevc\n")
        assert next_file(watcher) == video

    def test_removed_override_restores_location_config(self, watcher, location_config, location_dir):
        before = location_config.target_video_bitrate
        override = location_dir / Constants.FILE_CONFIG_OVERRIDE
        override.write_text("target_video_bitrate: 777\n")
        next_file(watcher)
        override.unlink()
        next_file(watcher)
        assert location_config.target_video_bitrate == before


class TestRunning:
    def test_running_file_is_not_ready(self, watcher, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        assert settled(watcher) == video
        assert watcher.scan() == []

    def test_two_files_of_a_location_run_at_once(self, watcher, location_dir):
        first, second = location_dir / "a.mp4", location_dir / "b.mp4"
        first.touch()
        second.touch()
        started = [settled(watcher), next_file(watcher)]
        assert set(started) == {first, second}
        watcher.finish(started[1], failed=True)
        watcher.finish(started[0], failed=False)
        state = json.loads(watcher.special_dirs.state_file.read_text())
        assert (list(state["failed"]), state["in_progress"]) == ([started[1].name], {})

    def test_both_open_attempts_count_after_the_service_died(self, location_config, location_dir):
        first, second = location_dir / "a.mp4", location_dir / "b.mp4"
        first.touch()
        second.touch()
        watcher = DirWatcher(location_config)
        settled(watcher)
        next_file(watcher)
        attempts = DirWatcher(location_config)._state.in_progress
        assert {key: attempt.attempts for key, attempt in attempts.items()} == {"a.mp4": 1, "b.mp4": 1}

    def test_abandoned_file_is_ready_again_and_counted(self, watcher, location_dir, caplog):
        """A job that died, e.g. by the OOM killer: like a service that died, it is given up on after a few."""
        video = location_dir / "movie.mp4"
        video.touch()
        assert settled(watcher) == video
        for _ in range(MAX_ATTEMPTS - 1):
            watcher.abandon(video)
            assert next_file(watcher) == video
        watcher.abandon(video)
        assert next_file(watcher) is None
        assert "cut off" in caplog.text

    def test_deleted_file_in_progress_is_forgotten(self, location_config, location_dir):
        video, other = location_dir / "movie.mp4", location_dir / "other.mp4"
        video.touch()
        other.touch()
        settled(DirWatcher(location_config)).unlink()
        watcher = DirWatcher(location_config)
        watcher.scan()
        assert list(watcher._state.in_progress) == []


def file_info(shall_encode: bool, is_valid: bool = True) -> FileInfo:
    return FileInfo(Path("movie.mp4"), is_valid=is_valid, video_stream=VideoStream(shall_encode=shall_encode))


class TestSlotOf:
    @pytest.fixture
    def analyzer(self) -> MagicMock:
        with patch("baycone_service.watcher.Analyzer") as analyzer:
            analyzer.return_value.analyze.return_value = file_info(shall_encode=False)
            yield analyzer

    @pytest.fixture
    def video(self, watcher, location_dir) -> Path:
        video = location_dir / "movie.mp4"
        video.touch()
        watcher.scan()
        return video

    @pytest.mark.parametrize(
        "info, slot",
        [
            (file_info(shall_encode=True), Slot.ENCODE),
            (file_info(shall_encode=False), Slot.COPY),
            (file_info(shall_encode=True, is_valid=False), Slot.COPY),
        ],
        ids=["encoded video", "copied video", "invalid file fails fast"],
    )
    def test_video_encode_decides(self, watcher, video, analyzer, info, slot):
        analyzer.return_value.analyze.return_value = info
        assert watcher.slot_of(video) == slot

    def test_probes_with_the_location_settings(self, watcher, video, analyzer, location_config):
        watcher.slot_of(video)
        assert analyzer.call_args.args[0] is location_config

    def test_probes_a_file_version_once(self, watcher, video, analyzer):
        watcher.slot_of(video)
        watcher.scan()
        watcher.slot_of(video)
        assert analyzer.return_value.analyze.call_count == 1

    def test_changed_file_is_probed_again(self, watcher, video, analyzer):
        watcher.slot_of(video)
        modify(video)
        watcher.scan()
        watcher.slot_of(video)
        assert analyzer.return_value.analyze.call_count == 2

    def test_changed_override_probes_again(self, watcher, video, analyzer, location_dir):
        watcher.slot_of(video)
        (location_dir / Constants.FILE_CONFIG_OVERRIDE).write_text("force_video_encode: true\n")
        watcher.scan()
        watcher.slot_of(video)
        assert analyzer.return_value.analyze.call_count == 2


class TestUpdateConfig:
    """A config reload while a job of the location may be running."""

    def test_takes_the_new_config_with_its_override(self, watcher, location_config, location_dir):
        (location_dir / Constants.FILE_CONFIG_OVERRIDE).write_text("target_video_bitrate: 777\n")
        reloaded = LocationConfig(path=location_config.path, limit_video_bitrate=5000)
        watcher.update_config(reloaded)
        assert (reloaded.limit_video_bitrate, reloaded.target_video_bitrate) == (5000, 777)
        assert watcher._location_config is reloaded

    def test_keeps_the_work_files_and_the_running_attempt(self, watcher, location_config, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        settled(watcher)
        encoded = watcher.special_dirs.encoded_dir / "movie.mp4"
        encoded.touch()
        watcher.update_config(LocationConfig(path=location_config.path))
        assert encoded.exists()
        assert watcher.scan() == []
        watcher.finish(video, failed=False)
        assert watcher.scan() == [video]

    def test_files_stay_ready(self, watcher, location_config, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        watcher.scan()
        watcher.update_config(LocationConfig(path=location_config.path))
        assert watcher.scan() == [video]

    def test_ends_a_pause(self, watcher, location_config, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        watcher.scan()
        watcher.pause()
        watcher.update_config(LocationConfig(path=location_config.path))
        assert watcher.scan() == [video]

    def test_probes_again(self, watcher, location_config, location_dir):
        video = location_dir / "movie.mp4"
        video.touch()
        watcher.scan()
        with patch("baycone_service.watcher.Analyzer") as analyzer:
            analyzer.return_value.analyze.return_value = file_info(shall_encode=False)
            watcher.slot_of(video)
            watcher.update_config(LocationConfig(path=location_config.path))
            watcher.slot_of(video)
        assert analyzer.return_value.analyze.call_count == 2
