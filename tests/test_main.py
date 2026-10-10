from unittest.mock import MagicMock, patch

import pytest

from baycone.__main__ import Service


@pytest.fixture
def service() -> Service:
    service = Service()
    service._encoder = MagicMock()
    return service


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
            patch.object(service, "_process_next", side_effect=process_next),
            patch("baycone.__main__.time.sleep") as mock_sleep,
        ):
            service.main_loop()
        mock_sleep.assert_called_once()


class TestStart:
    def test_fails_without_env_variables(self, service, monkeypatch):
        monkeypatch.delenv("BAYCONE_LOCATIONS", raising=False)
        monkeypatch.delenv("BAYCONE_CONFIG", raising=False)
        assert service.start() == 1
