import logging
import os
from copy import deepcopy
from typing import Iterator
from pathlib import Path
from dataclasses import asdict, dataclass, field, fields

import yaml

log = logging.getLogger(__name__)


class Serializable:

    def from_dict(self, data: dict) -> None:
        names = {f.name for f in fields(self)}
        for key, value in data.items():
            if key in names:
                setattr(self, key, deepcopy(value))
            else:
                log.warning(f"Unknown configuration key '{key}' ignored")

    def to_dict(self) -> dict:
        return asdict(self)


class Constants:
    DIR_TEMP = Path(".baycone")
    DIR_PASSLOG = DIR_TEMP / "passlogfiles"
    DIR_ENCODED = DIR_TEMP / "encoded"
    DIR_ORIGINALS = Path("_baycone_originals")
    DIR_PROCESSED = Path("_baycone_processed")

    SPECIAL_DIRS = (DIR_TEMP, DIR_PASSLOG, DIR_ENCODED, DIR_ORIGINALS, DIR_PROCESSED)
    FILE_CONFIG_OVERRIDE = DIR_TEMP / "config.override.yml"

    DIR_LOCATIONS = os.environ.get("BAYCONE_LOCATIONS")
    FILE_CONFIG = os.environ.get("BAYCONE_CONFIG")

    @staticmethod
    def get_missing_env_variables() -> list[str]:
        return [name for name in ("BAYCONE_LOCATIONS", "BAYCONE_CONFIG") if not os.environ.get(name)]


class SpecialDirs:
    def __init__(self, root_dir: Path):
        self._root_dir: Path = root_dir if root_dir.is_absolute() else Path(Constants.DIR_LOCATIONS) / root_dir

    def __iter__(self) -> Iterator[Path]:
        return iter((self.temp_dir, self.passlog_dir, self.encoded_dir, self.originals_dir, self.processed_dir))

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def temp_dir(self) -> Path:
        return self._root_dir / Constants.DIR_TEMP

    @property
    def passlog_dir(self) -> Path:
        return self._root_dir / Constants.DIR_PASSLOG

    @property
    def encoded_dir(self) -> Path:
        return self._root_dir / Constants.DIR_ENCODED

    @property
    def originals_dir(self) -> Path:
        return self._root_dir / Constants.DIR_ORIGINALS

    @property
    def processed_dir(self) -> Path:
        return self._root_dir / Constants.DIR_PROCESSED

    def exists(self) -> bool:
        return all(spec_dir.is_dir() for spec_dir in self)

    def create(self) -> None:
        for spec_dir in self:
            spec_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class DefaultsConfig(Serializable):
    allowed_extensions: list[str] = field(default_factory=lambda: [".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm"])
    allowed_containers: list[str] = field(default_factory=lambda: ["mp4"])
    allowed_video_codecs: list[str] = field(default_factory=lambda: ["h264"])
    allowed_audio_codecs: list[str] = field(default_factory=lambda: ["aac"])

    target_extension: str = ".mp4"
    target_container: str = "mp4"

    target_video_codec: str = "h264"
    target_video_bitrate: int = 2250

    target_audio_codec: str = "aac"
    target_audio_channel_bitrate: int = 64
    target_audio_languages: list[str] = field(default_factory=lambda: ["eng", "cze"])
    target_audio_channels: int = 2

    limit_video_bitrate: int = 2800
    limit_audio_channel_bitrate: int = 96


@dataclass
class LocationConfig(DefaultsConfig):
    path: str = ""

    @property
    def abspath(self) -> Path:
        return Path(Constants.DIR_LOCATIONS) / self.path


@dataclass
class Config(Serializable):
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    poll_interval: float = 5.0

    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    locations: list[LocationConfig] = field(default_factory=list)

    def from_dict(self, data: dict) -> None:
        data = dict(data)

        if "defaults" in data:
            self.defaults.from_dict(data.pop("defaults") or {})

        if "locations" in data:
            self.locations = []
            for loc_data in data.pop("locations") or []:
                loc_config = LocationConfig()
                loc_config.from_dict(self.defaults.to_dict())
                loc_config.from_dict(loc_data)
                self.locations.append(loc_config)

        super().from_dict(data)


config: Config = Config()


def load_config(file_path: Path) -> None:
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        if data:
            config.from_dict(data)


def load_config_override(location_config: LocationConfig, base: dict) -> None:
    """Resets `location_config` to `base`, then applies the location's override file, if any."""
    location_config.from_dict(base)

    override_path = location_config.abspath / Constants.FILE_CONFIG_OVERRIDE
    if not override_path.is_file():
        return

    try:
        data = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        log.error(f"Configuration override '{override_path}' ignored: {e}")
        return

    location_config.from_dict(data or {})
    log.info(f"Configuration override '{override_path}' applied")
