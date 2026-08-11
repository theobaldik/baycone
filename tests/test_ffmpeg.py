"""
Tests for baycone/ffmpeg.py

The command lists are templates with `{placeholder}` substitution, so these tests assert that every
placeholder is resolved and that the configured binary paths are honoured.
"""

from pathlib import Path

import pytest

from baycone import ffmpeg
from baycone.config import config

INPUT = Path("/media/movies/movie.mkv")
OUTPUT = Path("/media/movies/.baycone/encoded/movie.mp4")
PASSLOG = Path("/media/movies/.baycone/passlogfiles/movie.mkv")


@pytest.fixture
def default_binaries():
    ffmpeg_path, ffprobe_path = config.ffmpeg_path, config.ffprobe_path
    yield
    config.ffmpeg_path, config.ffprobe_path = ffmpeg_path, ffprobe_path


def all_commands() -> list[list[str]]:
    return [
        ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250),
        ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, 128),
        ffmpeg.get_cmd_optimize(INPUT, OUTPUT, "eng", "Stereo"),
        ffmpeg.get_cmd_ffprobe(INPUT),
    ]


class TestPlaceholders:
    def test_no_placeholder_is_left_unresolved(self):
        for cmd in all_commands():
            assert not [arg for arg in cmd if "{" in arg or "}" in arg]

    def test_paths_are_absolute(self):
        cmd = ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, 128)
        assert str(INPUT.absolute()) in cmd
        assert str(OUTPUT.absolute()) in cmd


class TestBinaryPaths:
    def test_defaults_to_path_lookup(self, default_binaries):
        assert ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250)[0] == "ffmpeg"
        assert ffmpeg.get_cmd_ffprobe(INPUT)[0] == "ffprobe"

    def test_ffmpeg_path_is_configurable(self, default_binaries):
        config.ffmpeg_path = "/opt/ffmpeg/bin/ffmpeg"
        for cmd in (
            ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250),
            ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, 128),
            ffmpeg.get_cmd_optimize(INPUT, OUTPUT, "eng", "Stereo"),
        ):
            assert cmd[0] == "/opt/ffmpeg/bin/ffmpeg"

    def test_ffprobe_path_is_configurable(self, default_binaries):
        config.ffprobe_path = "/opt/ffmpeg/bin/ffprobe"
        assert ffmpeg.get_cmd_ffprobe(INPUT)[0] == "/opt/ffmpeg/bin/ffprobe"


class TestCommandContent:
    def test_pass_1_discards_audio_and_output(self):
        cmd = ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250)
        assert "-an" in cmd
        assert "2250k" in cmd
        assert cmd[cmd.index("-pass") + 1] == "1"

    def test_pass_2_sets_both_bitrates(self):
        cmd = ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, 128)
        assert cmd[cmd.index("-b:v") + 1] == "2250k"
        assert cmd[cmd.index("-b:a") + 1] == "128k"
        assert cmd[cmd.index("-pass") + 1] == "2"

    def test_optimize_copies_streams_and_strips_metadata(self):
        cmd = ffmpeg.get_cmd_optimize(INPUT, OUTPUT, "eng", "Stereo")
        assert cmd[cmd.index("-c") + 1] == "copy"
        assert cmd[cmd.index("-map_metadata") + 1] == "-1"
        assert "language=eng" in cmd
        assert "title=Stereo" in cmd

    def test_faststart_is_requested(self):
        for cmd in (
            ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, 128),
            ffmpeg.get_cmd_optimize(INPUT, OUTPUT, "eng", "Stereo"),
        ):
            assert cmd[cmd.index("-movflags") + 1] == "+faststart"
