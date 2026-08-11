"""
Shared fixtures.

`Constants.DIR_LOCATIONS` is read from the environment at import time, so tests patch the class attribute
directly instead of the environment. Every location path in the config is resolved against it
(`LocationConfig.abspath`), which makes it the single knob needed to root a test location in `tmp_path`.
"""

from pathlib import Path

import pytest

from baycone.config import Constants, LocationConfig, config

LOCATION_NAME = "movies"


@pytest.fixture
def locations_root(tmp_path, monkeypatch) -> Path:
    """Roots `Constants.DIR_LOCATIONS` in `tmp_path` and returns it."""
    monkeypatch.setattr(Constants, "DIR_LOCATIONS", str(tmp_path))
    return tmp_path


@pytest.fixture
def location_config(locations_root) -> LocationConfig:
    """A `LocationConfig` registered in the global config, so `FileInfo` can resolve paths against it."""
    lc = LocationConfig(
        path=LOCATION_NAME,
        allowed_extensions=[".mp4", ".mkv"],
        allowed_containers=["mp4"],
        allowed_video_codecs=["h264"],
        allowed_audio_codecs=["aac"],
        target_video_bitrate=2250,
        target_audio_channel_bitrate=64,
        limit_video_bitrate=2800,
        limit_audio_channel_bitrate=96,
    )
    (locations_root / LOCATION_NAME).mkdir()
    config.locations = [lc]
    yield lc
    config.locations = []


@pytest.fixture
def location_dir(location_config) -> Path:
    """The watched directory of the test location."""
    return location_config.abspath
