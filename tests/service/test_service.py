import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone_service.service import Service
from baycone_service.config import Constants, config
from baycone.errors import InsufficientSpaceError
from baycone_service.watcher import FileChangeTracker


@pytest.fixture
def service() -> Service:
    service = Service()
    service._process_file = MagicMock()
    return service


def bump_mtime(file_path: Path) -> None:
    stat = file_path.stat()
    os.utime(file_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def make_watcher(file=None) -> MagicMock:
    watcher = MagicMock()
    watcher.get_new_file.return_value = file
    return watcher


class TestProcessNext:
    def test_idle_when_no_file_found(self, service):
        service._watchers = [make_watcher()]
        assert service._process_next() is False
        service._process_file.assert_not_called()

    def test_successful_file_is_not_marked_failed(self, service, tmp_path):
        watcher = make_watcher(tmp_path / "movie.mp4")
        service._watchers = [watcher]
        service._process_file.return_value = True
        assert service._process_next() is True
        watcher.finish.assert_called_once_with(tmp_path / "movie.mp4", False)

    def test_failed_file_is_marked_failed(self, service, tmp_path):
        video = tmp_path / "movie.mp4"
        watcher = make_watcher(video)
        service._watchers = [watcher]
        service._process_file.return_value = False
        service._process_next()
        watcher.finish.assert_called_once_with(video, True)

    def test_unexpected_error_does_not_stop_the_service(self, service, tmp_path):
        video = tmp_path / "movie.mp4"
        watcher = make_watcher(video)
        service._watchers = [watcher]
        service._process_file.side_effect = OSError("disk full")
        service._process_next()
        watcher.finish.assert_called_once_with(video, True)

    def test_file_waiting_for_free_space_pauses_the_location_without_failing(self, service, tmp_path):
        video = tmp_path / "movie.mp4"
        watcher = make_watcher(video)
        service._watchers = [watcher]
        service._process_file.side_effect = InsufficientSpaceError("needs 10.0 GiB")
        assert service._process_next() is True
        watcher.pause.assert_called_once()
        watcher.finish.assert_called_once_with(video, False)

    def test_other_locations_carry_on_while_one_waits_for_space(self, service, tmp_path):
        waiting, other = make_watcher(tmp_path / "big.mp4"), make_watcher(tmp_path / "small.mp4")
        service._watchers = [waiting, other]
        service._process_file.side_effect = [InsufficientSpaceError("needs 10.0 GiB"), True]
        service._process_next()
        other.finish.assert_called_once_with(tmp_path / "small.mp4", False)

    def test_stop_signal_ends_the_attempt_without_failing_the_file(self, service, tmp_path):
        """Only a service that died may leave the attempt open, a clean stop is not the file's fault."""
        video = tmp_path / "movie.mp4"
        watcher = make_watcher(video)
        service._watchers = [watcher]
        service._process_file.side_effect = KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            service._process_next()
        watcher.finish.assert_called_once_with(video, False)


class TestMainLoop:
    def test_sleeps_only_when_idle(self, service):
        results = iter([True, False])

        def process_next():
            try:
                return next(results)
            except StopIteration:
                service.stop()
                return True

        with (
            patch.object(service, "_reload_config"),
            patch.object(service, "_process_next", side_effect=process_next),
            patch("baycone_service.service.time.sleep") as mock_sleep,
        ):
            service.main_loop()
        mock_sleep.assert_called_once()

    def test_survives_an_unexpected_error(self, service, caplog):
        results = iter([RuntimeError("boom"), False])

        def process_next():
            result = next(results, None)
            if result is None:
                service.stop()
                return True
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch.object(service, "_reload_config"),
            patch.object(service, "_process_next", side_effect=process_next) as mock_process,
            patch("baycone_service.service.time.sleep") as mock_sleep,
        ):
            service.main_loop()
        assert mock_process.call_count == 3
        # Slept after the error too, a failure that repeats must not spin
        assert mock_sleep.call_count == 2
        assert "boom" in caplog.text

    def test_stop_signal_is_not_swallowed(self, service):
        with (
            patch.object(service, "_reload_config"),
            patch.object(service, "_process_next", side_effect=KeyboardInterrupt),
        ):
            with pytest.raises(KeyboardInterrupt):
                service.main_loop()

    def test_checks_config_on_every_iteration(self, service):
        results = iter([True, False])

        def process_next():
            if next(results, None) is None:
                service.stop()
            return True

        with (
            patch.object(service, "_reload_config") as mock_reload,
            patch.object(service, "_process_next", side_effect=process_next),
            patch("baycone_service.service.time.sleep"),
        ):
            service.main_loop()
        assert mock_reload.call_count == 3


class TestReloadConfig:
    @pytest.fixture
    def config_file(self, service, locations_root) -> Path:
        """The service started with a single 'movies' location."""
        config_file = locations_root / "config.yml"
        config_file.write_text("locations:\n  - path: movies\n")
        service._config_file = FileChangeTracker(config_file)
        service._config_file.changed()
        service._load_config()
        return config_file

    def test_unchanged_file_is_not_reloaded(self, service, config_file):
        with patch.object(service, "_load_config") as mock_load:
            service._reload_config()
        mock_load.assert_not_called()

    def test_changed_file_rebuilds_the_watchers(self, service, config_file):
        config_file.write_text("locations:\n  - path: movies\n  - path: shows\n")
        bump_mtime(config_file)
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies", "shows"]
        assert len(service._watchers) == 2

    def test_reload_retries_failed_files(self, service, config_file, locations_root):
        """The reload may be the fix, and it is the way to ask for a retry: `touch config.yml`."""
        video = locations_root / "movies" / "movie.mp4"
        video.touch()
        watcher = service._watchers[0]
        watcher.get_new_file()
        watcher.finish(watcher.get_new_file(), failed=True)

        bump_mtime(config_file)
        service._reload_config()

        service._watchers[0].get_new_file()
        assert service._watchers[0].get_new_file() == video

    @pytest.mark.parametrize(
        "content",
        ["- just\n- a list\n", "poll_interval: soon\n", "locations: movies\n", "defaults:\n  allowed_extensions: 5\n"],
        ids=["list", "wrong type", "locations not a list", "wrong list type"],
    )
    def test_wrongly_typed_file_keeps_the_previous_config(self, service, config_file, content):
        """Such a value would only blow up later, somewhere in the main loop."""
        watchers = service._watchers
        config_file.write_text(content)
        bump_mtime(config_file)
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert service._watchers is watchers

    def test_location_that_cannot_be_set_up_keeps_the_previous_config(self, service, config_file):
        """The config used to be swapped before the watchers were built, leaving the two out of step."""
        watchers = service._watchers
        defaults = config.defaults
        config_file.write_text("defaults:\n  target_video_bitrate: 777\nlocations:\n  - path: shows\n")
        bump_mtime(config_file)
        with patch("baycone_service.service.DirWatcher", side_effect=PermissionError("read-only")):
            service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert config.defaults is defaults
        assert service._watchers is watchers

    def test_invalid_file_keeps_the_previous_config(self, service, config_file):
        watchers = service._watchers
        config_file.write_text("locations: [\n")
        bump_mtime(config_file)
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert service._watchers is watchers

    def test_deleted_file_keeps_the_previous_config(self, service, config_file):
        config_file.unlink()
        service._reload_config()
        assert [lc.path for lc in config.locations] == ["movies"]
        assert len(service._watchers) == 1


class TestStart:
    def test_fails_without_env_variables(self, service, monkeypatch):
        monkeypatch.delenv("BAYCONE_LOCATIONS", raising=False)
        monkeypatch.delenv("BAYCONE_CONFIG", raising=False)
        assert service.start() == 1

    @pytest.mark.parametrize(
        "content", [None, "locations: [\n", "poll_interval: soon\n"], ids=["missing", "invalid", "wrong type"]
    )
    def test_fails_on_unreadable_config(self, service, monkeypatch, tmp_path, content):
        config_file = tmp_path / "config.yml"
        if content is not None:
            config_file.write_text(content)
        monkeypatch.setenv("BAYCONE_LOCATIONS", str(tmp_path))
        monkeypatch.setenv("BAYCONE_CONFIG", str(config_file))
        monkeypatch.setattr(Constants, "FILE_CONFIG", str(config_file))
        assert service.start() == 1
