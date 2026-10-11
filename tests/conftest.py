from copy import deepcopy
from dataclasses import fields
from pathlib import Path

import pytest

from baycone.config import Constants, LocationConfig, config

LOCATION_NAME = "movies"


@pytest.fixture(autouse=True)
def restore_config():
    """`load_config` replaces the global config, which would leak into later tests."""
    saved = {f.name: deepcopy(getattr(config, f.name)) for f in fields(config)}
    yield
    for name, value in saved.items():
        setattr(config, name, value)


@pytest.fixture
def locations_root(tmp_path, monkeypatch) -> Path:
    """Roots `Constants.DIR_LOCATIONS` in `tmp_path` and returns it."""
    monkeypatch.setattr(Constants, "DIR_LOCATIONS", str(tmp_path))
    return tmp_path


@pytest.fixture
def location_config(locations_root) -> LocationConfig:
    """Registered in the global config, which `FileInfo` resolves paths against."""
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
    return location_config.abspath
