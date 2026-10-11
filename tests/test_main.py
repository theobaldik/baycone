import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baycone.__main__ import Service
from baycone.config import Constants, config
from baycone.watcher import FileChangeTracker


@pytest.fixture
def service() -> Service:
    service = Service()
    service._encoder = MagicMock()
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
        service._encoder.process.assert_not_called()

    def test_successful_file_is_not_marked_failed(self, service, tmp_path):
        watcher = make_watcher(tmp_path / "movie.mp4")
        service._watchers = [watcher]
        service._encoder.process.return_value = True
        assert service._process_next() is True
        watcher.mark_failed.assert_not_called()

    def test_failed_file_is_marked_failed(self, service, tmp_path):
        video = tmp_path / "movie.mp4"
        watcher = make_watcher(video)
        service._watchers = [watcher]
        service._encoder.process.return_value = False
        service._process_next()
        watcher.mark_failed.assert_called_once_with(video)

    def test_unexpected_error_does_not_stop_the_service(self, service, tmp_path):
        video = tmp_path / "movie.mp4"
        watcher = make_watcher(video)
        service._watchers = [watcher]
        service._encoder.process.side_effect = OSError("disk full")
        service._process_next()
        watcher.mark_failed.assert_called_once_with(video)


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
            patch("baycone.__main__.time.sleep") as mock_sleep,
        ):
            service.main_loop()
        mock_sleep.assert_called_once()

    def test_checks_config_on_every_iteration(self, service):
        results = iter([True, False])

        def process_next():
            if next(results, None) is None:
                service.stop()
            return True

        with (
            patch.object(service, "_reload_config") as mock_reload,
            patch.object(service, "_process_next", side_effect=process_next),
            patch("baycone.__main__.time.sleep"),
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

    @pytest.mark.parametrize("content", [None, "locations: [\n"], ids=["missing", "invalid"])
    def test_fails_on_unreadable_config(self, service, monkeypatch, tmp_path, content):
        config_file = tmp_path / "config.yml"
        if content is not None:
            config_file.write_text(content)
        monkeypatch.setenv("BAYCONE_LOCATIONS", str(tmp_path))
        monkeypatch.setenv("BAYCONE_CONFIG", str(config_file))
        monkeypatch.setattr(Constants, "FILE_CONFIG", str(config_file))
        assert service.start() == 1
