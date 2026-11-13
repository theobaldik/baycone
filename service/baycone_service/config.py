import logging
import os
import shutil
from typing import Iterator
from pathlib import Path
from dataclasses import dataclass, field, fields

import yaml

from baycone.encoding import Job
from baycone.settings import EncodingSettings, FFmpegTools, Serializable

log = logging.getLogger(__name__)


class Constants:
    DIR_TEMP = Path(".baycone")
    DIR_PASSLOG = DIR_TEMP / Job.PASSLOG_DIR
    DIR_ENCODED = DIR_TEMP / Job.ENCODED_DIR
    DIR_OPTIMIZED = DIR_TEMP / Job.OPTIMIZED_DIR
    DIR_ORIGINALS = Path("_baycone_originals")
    DIR_PROCESSED = Path("_baycone_processed")

    FILE_CONFIG_OVERRIDE = DIR_TEMP / "config.override.yml"
    FILE_STATE = DIR_TEMP / "state.json"

    DIR_LOCATIONS = os.environ.get("BAYCONE_LOCATIONS")
    FILE_CONFIG = os.environ.get("BAYCONE_CONFIG")

    @staticmethod
    def get_missing_env_variables() -> list[str]:
        return [name for name in ("BAYCONE_LOCATIONS", "BAYCONE_CONFIG") if not os.environ.get(name)]


class SpecialDirs:
    def __init__(self, root_dir: Path):
        self._root_dir: Path = root_dir if root_dir.is_absolute() else Path(Constants.DIR_LOCATIONS) / root_dir

    def __iter__(self) -> Iterator[Path]:
        return iter(
            (
                self.temp_dir,
                self.passlog_dir,
                self.encoded_dir,
                self.optimized_dir,
                self.originals_dir,
                self.processed_dir,
            )
        )

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
    def optimized_dir(self) -> Path:
        """The finished file, before it is renamed into `processed_dir`."""
        return self._root_dir / Constants.DIR_OPTIMIZED

    @property
    def state_file(self) -> Path:
        return self._root_dir / Constants.FILE_STATE

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

    @staticmethod
    def _clear(directory: Path) -> None:
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)

    def clear_workdirs(self) -> None:
        """The passlog is not part of it: a finished first pass outlives an interrupted run."""
        self._clear(self.encoded_dir)
        self._clear(self.optimized_dir)

    def clear_passlog(self) -> None:
        self._clear(self.passlog_dir)


@dataclass
class DefaultsConfig(EncodingSettings):
    allowed_extensions: list[str] = field(default_factory=lambda: [".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm"])


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

    def tools(self) -> FFmpegTools:
        return FFmpegTools(ffmpeg=self.ffmpeg_path, ffprobe=self.ffprobe_path)

    def from_dict(self, data: dict) -> None:
        data = dict(data)

        if "defaults" in data:
            self.defaults.from_dict(data.pop("defaults") or {})

        if "locations" in data:
            locations = data.pop("locations") or []
            if not isinstance(locations, list):
                raise ValueError(f"Expected a list of locations, got {locations!r}")

            self.locations = []
            for loc_data in locations:
                loc_config = LocationConfig()
                loc_config.from_dict(self.defaults.to_dict())
                loc_config.from_dict(loc_data)
                self.locations.append(loc_config)

        super().from_dict(data)


config: Config = Config()


def load_config(file_path: Path) -> None:
    """Replaces `config` in place: keys removed from the file fall back to defaults, a failure leaves it untouched."""
    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    # An empty file is more likely caught mid-save than meant to drop every location
    if not data:
        return
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping of configuration keys, got {data!r}")

    loaded = Config()
    loaded.from_dict(data)
    for config_field in fields(Config):
        setattr(config, config_field.name, getattr(loaded, config_field.name))


def load_config_override(location_config: LocationConfig, base: dict) -> None:
    """Resets `location_config` to `base`, then applies the location's override file, if any."""
    location_config.from_dict(base)

    override_path = location_config.abspath / Constants.FILE_CONFIG_OVERRIDE
    if not override_path.is_file():
        return

    try:
        data = yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
        # The watcher and the special directories are bound to the path the location started with
        if isinstance(data, dict) and "path" in data:
            raise ValueError("'path' cannot be overridden")
        location_config.from_dict(data)
    except (OSError, ValueError, yaml.YAMLError) as e:
        location_config.from_dict(base)
        log.error(f"Configuration override '{override_path}' ignored: {e}")
        return

    log.info(f"Configuration override '{override_path}' applied")
