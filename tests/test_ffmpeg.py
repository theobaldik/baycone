from pathlib import Path

import pytest

from baycone import ffmpeg
from baycone.audio import AudioStream, AudioTrack
from baycone.config import config

INPUT = Path("/media/movies/movie.mkv")
OUTPUT = Path("/media/movies/.baycone/encoded/movie.mp4")
PASSLOG = Path("/media/movies/.baycone/passlogfiles/movie.mkv")

STEREO = AudioTrack(AudioStream(index=2, channels=2, lang="ces"), bitrate=128, default=True)
SURROUND = AudioTrack(AudioStream(index=0, channels=6, lang="eng", original=True))
TRACKS = [STEREO, SURROUND]


@pytest.fixture
def default_binaries():
    ffmpeg_path, ffprobe_path = config.ffmpeg_path, config.ffprobe_path
    yield
    config.ffmpeg_path, config.ffprobe_path = ffmpeg_path, ffprobe_path


def all_commands() -> list[list[str]]:
    return [
        ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250, []),
        ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, [], TRACKS),
        ffmpeg.get_cmd_encode_audio(INPUT, OUTPUT, TRACKS),
        ffmpeg.get_cmd_optimize(INPUT, OUTPUT, TRACKS, encoded=False),
        ffmpeg.get_cmd_cropdetect(INPUT),
        ffmpeg.get_cmd_ffprobe(INPUT),
    ]


def value_of(cmd: list[str], option: str) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == option]


class TestPlaceholders:
    def test_no_placeholder_is_left_unresolved(self):
        for cmd in all_commands():
            assert not [arg for arg in cmd if "{" in arg or "}" in arg]

    def test_paths_are_absolute(self):
        cmd = ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, [], TRACKS)
        assert str(INPUT.absolute()) in cmd
        assert str(OUTPUT.absolute()) in cmd


class TestBinaryPaths:
    def test_defaults_to_path_lookup(self, default_binaries):
        assert ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250, [])[0] == "ffmpeg"
        assert ffmpeg.get_cmd_ffprobe(INPUT)[0] == "ffprobe"

    def test_ffmpeg_path_is_configurable(self, default_binaries):
        config.ffmpeg_path = "/opt/ffmpeg/bin/ffmpeg"
        for cmd in all_commands()[:-1]:
            assert cmd[0] == "/opt/ffmpeg/bin/ffmpeg"

    def test_ffprobe_path_is_configurable(self, default_binaries):
        config.ffprobe_path = "/opt/ffmpeg/bin/ffprobe"
        assert ffmpeg.get_cmd_ffprobe(INPUT)[0] == "/opt/ffmpeg/bin/ffprobe"


class TestCommandContent:
    def test_pass_1_discards_audio_and_output(self):
        cmd = ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250, [])
        assert "-an" in cmd
        assert "2250k" in cmd
        assert cmd[cmd.index("-pass") + 1] == "1"

    def test_pass_2_sets_video_bitrate(self):
        cmd = ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, [], TRACKS)
        assert cmd[cmd.index("-b:v") + 1] == "2250k"
        assert cmd[cmd.index("-pass") + 1] == "2"

    def test_audio_encode_copies_video(self):
        cmd = ffmpeg.get_cmd_encode_audio(INPUT, OUTPUT, TRACKS)
        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert "-pass" not in cmd

    def test_subtitles_are_never_mapped(self):
        for cmd in all_commands():
            assert not [arg for arg in cmd if arg.startswith("0:s")]
            assert "0" not in value_of(cmd, "-map")

    def test_cropdetect_reads_keyframes_of_the_first_video_stream(self):
        cmd = ffmpeg.get_cmd_cropdetect(INPUT)
        assert value_of(cmd, "-skip_frame") == ["nokey"]
        assert value_of(cmd, "-map") == ["0:v:0"]
        assert value_of(cmd, "-vf")[0].startswith("cropdetect=")

    def test_faststart_is_requested(self):
        for cmd in (
            ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, [], TRACKS),
            ffmpeg.get_cmd_optimize(INPUT, OUTPUT, TRACKS, encoded=False),
        ):
            assert cmd[cmd.index("-movflags") + 1] == "+faststart"


class TestAudioEncodeArgs:
    @pytest.mark.parametrize(
        "cmd",
        [
            ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, [], TRACKS),
            ffmpeg.get_cmd_encode_audio(INPUT, OUTPUT, TRACKS),
        ],
    )
    def test_maps_source_streams_in_output_order(self, cmd):
        assert value_of(cmd, "-map") == ["0:v:0", "0:a:2", "0:a:0"]

    def test_encodes_only_tracks_with_a_bitrate(self):
        cmd = ffmpeg.get_cmd_encode_audio(INPUT, OUTPUT, TRACKS)
        assert value_of(cmd, "-c:a:0") == ["aac"]
        assert value_of(cmd, "-b:a:0") == ["128k"]
        assert value_of(cmd, "-c:a:1") == ["copy"]
        assert "-b:a:1" not in cmd


class TestAudioMetadataArgs:
    def test_copies_streams_and_strips_metadata(self):
        cmd = ffmpeg.get_cmd_optimize(INPUT, OUTPUT, TRACKS, encoded=False)
        assert cmd[cmd.index("-c") + 1] == "copy"
        assert cmd[cmd.index("-map_metadata") + 1] == "-1"

    def test_maps_source_streams_when_not_encoded(self):
        cmd = ffmpeg.get_cmd_optimize(INPUT, OUTPUT, TRACKS, encoded=False)
        assert value_of(cmd, "-map") == ["0:v", "0:a:2", "0:a:0"]

    def test_maps_by_position_when_encoded(self):
        cmd = ffmpeg.get_cmd_optimize(INPUT, OUTPUT, TRACKS, encoded=True)
        assert value_of(cmd, "-map") == ["0:v", "0:a:0", "0:a:1"]

    def test_tags_every_track(self):
        cmd = ffmpeg.get_cmd_optimize(INPUT, OUTPUT, TRACKS, encoded=False)
        assert value_of(cmd, "-metadata:s:a:0") == ["language=ces", "title=Stereo"]
        assert value_of(cmd, "-metadata:s:a:1") == ["language=eng", "title=Surround 5.1"]

    def test_sets_dispositions(self):
        cmd = ffmpeg.get_cmd_optimize(INPUT, OUTPUT, TRACKS, encoded=False)
        assert value_of(cmd, "-disposition:a:0") == ["default"]
        assert value_of(cmd, "-disposition:a:1") == ["original"]


class TestVideoArgs:
    FILTERS = [ffmpeg.get_filter_crop(800, 140), ffmpeg.get_filter_scale(1920, 1080)]

    def test_both_passes_output_8_bit(self):
        for cmd in (
            ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250, []),
            ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, [], TRACKS),
        ):
            assert value_of(cmd, "-pix_fmt") == ["yuv420p"]

    def test_both_passes_apply_the_same_filters(self):
        pass_1 = ffmpeg.get_cmd_encode_movie_pass_1(INPUT, PASSLOG, 2250, self.FILTERS)
        pass_2 = ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, self.FILTERS, TRACKS)
        expected = ["crop=iw:800:0:140,scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2"]
        assert value_of(pass_1, "-vf") == value_of(pass_2, "-vf") == expected

    def test_no_filter_option_without_filters(self):
        assert "-vf" not in ffmpeg.get_cmd_encode_movie_pass_2(INPUT, OUTPUT, PASSLOG, 2250, [], TRACKS)

    def test_tonemap_states_the_source_transfer(self):
        assert ffmpeg.get_filter_tonemap("arib-std-b67").startswith("zscale=tin=arib-std-b67:")
