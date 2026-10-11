from pathlib import Path

import pytest
import yaml

from baycone.config import Config, Constants, DefaultsConfig, LocationConfig, SpecialDirs, load_config
from baycone import config as config_module


def parse(yaml_text: str) -> Config:
    cfg = Config()
    cfg.from_dict(yaml.safe_load(yaml_text))
    return cfg


class TestDefaultsInheritance:
    def test_location_inherits_defaults(self):
        cfg = parse("""
            defaults:
              target_video_bitrate: 1500
              allowed_extensions: [.mkv]
            locations:
              - path: movies
            """)
        assert cfg.locations[0].target_video_bitrate == 1500
        assert cfg.locations[0].allowed_extensions == [".mkv"]

    def test_location_overrides_win_over_defaults(self):
        cfg = parse("""
            defaults:
              target_video_bitrate: 1500
            locations:
              - path: movies
                target_video_bitrate: 900
            """)
        assert cfg.locations[0].target_video_bitrate == 900

    def test_location_override_does_not_leak_to_siblings(self):
        cfg = parse("""
            defaults:
              target_video_bitrate: 1500
            locations:
              - path: movies
                target_video_bitrate: 900
              - path: tv-shows
            """)
        assert cfg.locations[0].target_video_bitrate == 900
        assert cfg.locations[1].target_video_bitrate == 1500

    def test_location_override_does_not_mutate_defaults(self):
        cfg = parse("""
            defaults:
              allowed_extensions: [.mkv]
            locations:
              - path: movies
                allowed_extensions: [.mp4]
            """)
        cfg.locations[0].allowed_extensions.append(".avi")
        assert cfg.defaults.allowed_extensions == [".mkv"]

    def test_falls_back_to_builtin_defaults(self):
        cfg = parse("locations:\n  - path: movies\n")
        assert cfg.locations[0].target_video_bitrate == DefaultsConfig().target_video_bitrate
        assert cfg.locations[0].allowed_containers == DefaultsConfig().allowed_containers

    def test_path_is_not_overwritten_by_defaults(self):
        cfg = parse("defaults:\n  target_video_bitrate: 1500\nlocations:\n  - path: movies\n")
        assert cfg.locations[0].path == "movies"


class TestConfig:
    def test_binary_paths_and_poll_interval(self):
        cfg = parse("ffmpeg_path: /opt/ffmpeg\nffprobe_path: /opt/ffprobe\npoll_interval: 2.5\n")
        assert cfg.ffmpeg_path == "/opt/ffmpeg"
        assert cfg.ffprobe_path == "/opt/ffprobe"
        assert cfg.poll_interval == 2.5

    def test_defaults_when_file_is_minimal(self):
        cfg = parse("locations:\n  - path: movies\n")
        assert cfg.ffmpeg_path == "ffmpeg"
        assert cfg.ffprobe_path == "ffprobe"
        assert cfg.poll_interval == 5.0

    def test_unknown_keys_are_ignored(self):
        cfg = parse("nonsense: 1\nlocations:\n  - path: movies\n")
        assert len(cfg.locations) == 1

    def test_empty_sections_are_ignored(self):
        cfg = parse("defaults:\nlocations:\n")
        assert cfg.locations == []

    def test_subtitles_are_off_by_default_and_overridable_per_location(self):
        cfg = parse(
            "defaults:\n  target_subtitle_languages: [eng]\n"
            "locations:\n  - path: a\n  - path: b\n    target_subtitle_languages: [cze]\n"
        )
        assert DefaultsConfig().target_subtitle_languages == []
        assert [lc.target_subtitle_languages for lc in cfg.locations] == [["eng"], ["cze"]]

    def test_roundtrips_through_to_dict(self):
        cfg = parse("defaults:\n  target_video_bitrate: 1500\nlocations:\n  - path: movies\n")
        restored = Config()
        restored.from_dict(cfg.to_dict())
        assert restored.to_dict() == cfg.to_dict()


class TestLoadConfig:
    def test_loads_from_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("poll_interval: 9.0\nlocations:\n  - path: movies\n")
        monkeypatch.setattr(config_module.config, "locations", [])

        load_config(cfg_file)

        assert config_module.config.poll_interval == 9.0
        assert config_module.config.locations[0].path == "movies"

    def test_empty_file_leaves_config_untouched(self, tmp_path):
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("")
        load_config(cfg_file)  # must not raise


class TestLoadConfigOverride:
    @pytest.fixture
    def override(self, location_dir) -> Path:
        path = location_dir / Constants.FILE_CONFIG_OVERRIDE
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_applies_override_file(self, location_config, override):
        override.write_text("target_video_bitrate: 777\n")
        config_module.load_config_override(location_config, location_config.to_dict())
        assert location_config.target_video_bitrate == 777

    def test_no_override_file_is_a_noop(self, location_config):
        before = location_config.target_video_bitrate
        config_module.load_config_override(location_config, location_config.to_dict())
        assert location_config.target_video_bitrate == before

    def test_removed_override_restores_base(self, location_config, override):
        base = location_config.to_dict()
        override.write_text("target_video_bitrate: 777\n")
        config_module.load_config_override(location_config, base)
        override.unlink()
        config_module.load_config_override(location_config, base)
        assert location_config.target_video_bitrate == base["target_video_bitrate"]

    def test_empty_override_file_is_ignored(self, location_config, override):
        override.write_text("")
        config_module.load_config_override(location_config, location_config.to_dict())

    def test_invalid_override_file_is_ignored(self, location_config, override):
        before = location_config.target_video_bitrate
        override.write_text("target_video_bitrate: [unclosed\n")
        config_module.load_config_override(location_config, location_config.to_dict())
        assert location_config.target_video_bitrate == before


class TestSpecialDirs:
    def test_create_and_exists(self, tmp_path):
        dirs = SpecialDirs(tmp_path)
        assert dirs.exists() is False
        dirs.create()
        assert dirs.exists() is True

    def test_layout(self, tmp_path):
        dirs = SpecialDirs(tmp_path)
        assert dirs.root_dir == tmp_path
        assert dirs.temp_dir == tmp_path / Constants.DIR_TEMP
        assert dirs.passlog_dir == tmp_path / Constants.DIR_PASSLOG
        assert dirs.encoded_dir == tmp_path / Constants.DIR_ENCODED
        assert dirs.originals_dir == tmp_path / Constants.DIR_ORIGINALS
        assert dirs.processed_dir == tmp_path / Constants.DIR_PROCESSED

    def test_relative_root_resolves_against_locations_dir(self, locations_root):
        dirs = SpecialDirs(Path("movies"))
        assert dirs.root_dir == locations_root / "movies"

    def test_iterates_all_special_dirs(self, tmp_path):
        assert len(list(SpecialDirs(tmp_path))) == len(Constants.SPECIAL_DIRS)


class TestLocationConfig:
    def test_abspath_resolves_against_locations_dir(self, locations_root):
        assert LocationConfig(path="movies").abspath == locations_root / "movies"

    def test_absolute_path_is_kept(self, locations_root, tmp_path):
        absolute = tmp_path / "elsewhere"
        assert LocationConfig(path=str(absolute)).abspath == absolute


class TestConstants:
    def test_reports_missing_env_variables(self, monkeypatch):
        monkeypatch.delenv("BAYCONE_LOCATIONS", raising=False)
        monkeypatch.delenv("BAYCONE_CONFIG", raising=False)
        assert Constants.get_missing_env_variables() == ["BAYCONE_LOCATIONS", "BAYCONE_CONFIG"]

    def test_reports_nothing_when_env_is_set(self, monkeypatch):
        monkeypatch.setenv("BAYCONE_LOCATIONS", "/locations")
        monkeypatch.setenv("BAYCONE_CONFIG", "/config/config.yml")
        assert Constants.get_missing_env_variables() == []
