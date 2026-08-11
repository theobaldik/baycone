import os
from typing import Iterator
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import yaml


class Serializable(ABC):

    @abstractmethod
    def from_dict(self, data: dict) -> None:
        """Overrides attributes specified `data` by their values."""
        pass

    @abstractmethod
    def to_dict(self) -> dict:
        """Returns `dict` representation of the object."""
        pass


class Constants:
    DIR_TEMP = ".baycone"
    DIR_PASSLOG = os.path.join(DIR_TEMP, "passlogfiles")
    DIR_ENCODED = os.path.join(DIR_TEMP, "encoded")
    DIR_ORIGINALS = "_baycone_originals"
    DIR_PROCESSED = "_baycone_processed"

    SPECIAL_DIRS = (DIR_TEMP, DIR_PASSLOG, DIR_ENCODED, DIR_ORIGINALS, DIR_PROCESSED)
    FILE_CONFIG_OVERRIDE = os.path.join(DIR_TEMP, "config.override.yml")

    DIR_LOCATIONS = os.environ.get("BAYCONE_LOCATIONS")
    FILE_CONFIG = os.environ.get("BAYCONE_CONFIG")

    @staticmethod
    def get_missing_env_variables() -> list[str]:
        """Returns list of missing environment variable names."""
        result = []
        if not os.environ.get("BAYCONE_LOCATIONS"):
            result.append("BAYCONE_LOCATIONS")
        if not os.environ.get("BAYCONE_CONFIG"):
            result.append("BAYCONE_CONFIG")

        return result


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
    # TODO: Not honoured yet - the audio track language is hardcoded until multitrack audio is implemented.
    target_audio_languages: list[str] = field(default_factory=lambda: ["eng", "cze"])

    limit_video_bitrate: int = 2800
    limit_audio_channel_bitrate: int = 96

    def from_dict(self, data: dict) -> None:
        self.allowed_extensions = data.get("allowed_extensions", self.allowed_extensions)
        self.allowed_containers = data.get("allowed_containers", self.allowed_containers)
        self.allowed_video_codecs = data.get("allowed_video_codecs", self.allowed_video_codecs)
        self.allowed_audio_codecs = data.get("allowed_audio_codecs", self.allowed_audio_codecs)

        self.target_extension = data.get("target_extension", self.target_extension)
        self.target_container = data.get("target_container", self.target_container)

        self.target_video_codec = data.get("target_video_codec", self.target_video_codec)
        self.target_video_bitrate = data.get("target_video_bitrate", self.target_video_bitrate)

        self.target_audio_codec = data.get("target_audio_codec", self.target_audio_codec)
        self.target_audio_channel_bitrate = data.get("target_audio_channel_bitrate", self.target_audio_channel_bitrate)
        self.target_audio_languages = data.get("target_audio_languages", self.target_audio_languages)

        self.limit_video_bitrate = data.get("limit_video_bitrate", self.limit_video_bitrate)
        self.limit_audio_channel_bitrate = data.get("limit_audio_channel_bitrate", self.limit_audio_channel_bitrate)

    def to_dict(self) -> dict:
        return {
            "allowed_extensions": self.allowed_extensions,
            "allowed_containers": self.allowed_containers,
            "allowed_video_codecs": self.allowed_video_codecs,
            "allowed_audio_codecs": self.allowed_audio_codecs,
            "target_extension": self.target_extension,
            "target_container": self.target_container,
            "target_video_codec": self.target_video_codec,
            "target_video_bitrate": self.target_video_bitrate,
            "target_audio_codec": self.target_audio_codec,
            "target_audio_channel_bitrate": self.target_audio_channel_bitrate,
            "target_audio_languages": self.target_audio_languages,
            "limit_video_bitrate": self.limit_video_bitrate,
            "limit_audio_channel_bitrate": self.limit_audio_channel_bitrate,
        }


@dataclass
class LocationConfig(DefaultsConfig):
    path: str = ""

    def from_dict(self, data: dict) -> None:
        super().from_dict(data)
        self.path = data.get("path", self.path)

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict["path"] = self.path
        return base_dict

    @property
    def abspath(self) -> Path:
        return Path(Constants.DIR_LOCATIONS) / Path(self.path)


@dataclass
class Config(Serializable):
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    poll_interval: float = 5.0

    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    locations: list[LocationConfig] = field(default_factory=list)

    def from_dict(self, data: dict):
        self.ffmpeg_path = data.get("ffmpeg_path", self.ffmpeg_path)
        self.ffprobe_path = data.get("ffprobe_path", self.ffprobe_path)
        self.poll_interval = data.get("poll_interval", self.poll_interval)

        if "defaults" in data:
            self.defaults.from_dict(data["defaults"])

        if "locations" in data:
            self.locations = []
            for loc_data in data["locations"]:
                loc_config = LocationConfig()
                # Every location inherits the defaults first, then applies its own overrides on top.
                loc_config.from_dict(self.defaults.to_dict())
                loc_config.from_dict(loc_data)
                self.locations.append(loc_config)

    def to_dict(self) -> dict:
        return {
            "ffmpeg_path": self.ffmpeg_path,
            "ffprobe_path": self.ffprobe_path,
            "poll_interval": self.poll_interval,
            "defaults": self.defaults.to_dict(),
            "locations": [loc.to_dict() for loc in self.locations],
        }


config: Config = Config()


def load_config(file_path: Path) -> None:
    """Loads the configuration from a YAML file and updates the global `config` object."""
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        if data:
            config.from_dict(data)


def load_config_override(location_config: LocationConfig) -> None:
    """Loads the configuration override of a location from a YAML file."""
    override_path = location_config.abspath / Path(Constants.FILE_CONFIG_OVERRIDE)
    if override_path.is_file():
        for loc in config.locations:
            if loc.path == location_config.path:
                with open(override_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    loc.from_dict(data)
                    return
