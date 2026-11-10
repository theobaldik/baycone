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

    def test_removed_key_falls_back_to_default(self, tmp_path):
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("poll_interval: 9.0\ndefaults:\n  target_video_bitrate: 1500\n")
        load_config(cfg_file)
        cfg_file.write_text("locations:\n  - path: movies\n")
        load_config(cfg_file)

        assert config_module.config.poll_interval == Config().poll_interval
        assert config_module.config.defaults.target_video_bitrate == DefaultsConfig().target_video_bitrate
        assert config_module.config.locations[0].target_video_bitrate == DefaultsConfig().target_video_bitrate

    def test_invalid_file_leaves_config_untouched(self, tmp_path):
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("poll_interval: 9.0\n")
        load_config(cfg_file)
        cfg_file.write_text("locations: [\n")

        with pytest.raises(yaml.YAMLError):
            load_config(cfg_file)
        assert config_module.config.poll_interval == 9.0


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

    @pytest.mark.parametrize(
        "content",
        [
            "- just\n- a list\n",
            "just text\n",
            "allowed_extensions: 5\n",
            "allowed_extensions: [5]\n",
            "target_video_bitrate: fast\n",
            "path: elsewhere\n",
        ],
        ids=["list", "scalar", "not a list", "list of numbers", "not a number", "path"],
    )
    def test_wrongly_typed_override_file_is_ignored(self, location_config, override, content, caplog):
        """Such a file used to raise in the middle of a scan and take the service down."""
        base = location_config.to_dict()
        override.write_text(content)
        config_module.load_config_override(location_config, base)
        assert location_config.to_dict() == base
        assert "ignored" in caplog.text

    def test_override_with_one_bad_key_is_ignored_as_a_whole(self, location_config, override):
        base = location_config.to_dict()
        override.write_text("target_video_bitrate: 777\nallowed_extensions: 5\n")
        config_module.load_config_override(location_config, base)
        assert location_config.to_dict() == base

    def test_override_that_is_not_utf8_is_ignored(self, location_config, override):
        base = location_config.to_dict()
        override.write_bytes(b"target_video_codec: \xff\xfe\n")
        config_module.load_config_override(location_config, base)
        assert location_config.to_dict() == base

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
        assert dirs.optimized_dir == tmp_path / Constants.DIR_OPTIMIZED
        assert dirs.state_file == tmp_path / Constants.FILE_STATE
        assert dirs.originals_dir == tmp_path / Constants.DIR_ORIGINALS
        assert dirs.processed_dir == tmp_path / Constants.DIR_PROCESSED

    def test_relative_root_resolves_against_locations_dir(self, locations_root):
        dirs = SpecialDirs(Path("movies"))
        assert dirs.root_dir == locations_root / "movies"

    def test_iterates_all_special_dirs(self, tmp_path):
        dirs = SpecialDirs(tmp_path)
        assert set(dirs) == {
            dirs.temp_dir,
            dirs.passlog_dir,
            dirs.encoded_dir,
            dirs.optimized_dir,
            dirs.originals_dir,
            dirs.processed_dir,
        }

    def test_clear_workdirs_empties_the_intermediate_files_only(self, tmp_path):
        dirs = SpecialDirs(tmp_path)
        dirs.create()
        kept = [
            dirs.passlog_dir / "passlog-0.log",
            dirs.state_file,
            tmp_path / Constants.FILE_CONFIG_OVERRIDE,
            dirs.originals_dir / "movie.mkv",
            dirs.processed_dir / "movie.mp4",
            tmp_path / "pending.mkv",
        ]
        removed = [dirs.encoded_dir / "movie.mp4", dirs.optimized_dir / "Show" / "episode.mp4"]
        for file_path in kept + removed:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.touch()

        dirs.clear_workdirs()

        assert all(file_path.is_file() for file_path in kept)
        assert not any(file_path.exists() for file_path in removed)
        assert dirs.encoded_dir.is_dir() and dirs.optimized_dir.is_dir()

    def test_clear_passlog(self, tmp_path):
        dirs = SpecialDirs(tmp_path)
        dirs.create()
        (dirs.passlog_dir / "passlog-0.log").touch()
        dirs.clear_passlog()
        assert list(dirs.passlog_dir.iterdir()) == []


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


class TestValidation:
    @pytest.mark.parametrize(
        "yaml_text",
        [
            "poll_interval: soon",
            "poll_interval: 0",
            "poll_interval: -5",
            "poll_interval: true",
            "ffmpeg_path: 5",
            "defaults: [a, b]",
            "defaults:\n  allowed_extensions: .mkv",
            "defaults:\n  allowed_extensions: [1, 2]",
            "defaults:\n  target_video_bitrate: high",
            "defaults:\n  target_video_bitrate: 0",
            "defaults:\n  check_free_space: 1",
            "defaults:\n  target_extension: [.mp4]",
            "locations: movies",
            "locations:\n  - movies",
            "locations:\n  - path: 5",
            "locations:\n  - path: movies\n    limit_video_height: tall",
        ],
    )
    def test_wrong_value_is_rejected(self, yaml_text):
        with pytest.raises(ValueError):
            parse(yaml_text)

    def test_error_names_the_key(self):
        with pytest.raises(ValueError, match="target_video_bitrate"):
            parse("defaults:\n  target_video_bitrate: high")

    def test_whole_number_is_fine_for_a_fractional_setting(self):
        assert parse("poll_interval: 10").poll_interval == 10

    def test_fraction_is_fine_for_a_bitrate(self):
        assert parse("defaults:\n  target_video_bitrate: 2250.0").defaults.target_video_bitrate == 2250.0

    def test_empty_list_is_fine(self):
        assert parse("defaults:\n  target_subtitle_languages: []").defaults.target_subtitle_languages == []

    def test_free_space_check_is_on_by_default_and_overridable_per_location(self):
        cfg = parse("locations:\n  - path: movies\n  - path: nas\n    check_free_space: false")
        assert [lc.check_free_space for lc in cfg.locations] == [True, False]

    def test_file_that_is_not_a_mapping_leaves_config_untouched(self, tmp_path):
        config_file = tmp_path / "config.yml"
        config_file.write_text("- just\n- a list\n")
        before = config_module.config.to_dict()
        with pytest.raises(ValueError):
            load_config(config_file)
        assert config_module.config.to_dict() == before
