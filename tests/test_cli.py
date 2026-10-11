import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from baycone import cli

PROBE_STEREO = {
    "format": {"duration": "10"},
    "streams": [{"codec_type": "video"}, {"codec_type": "audio", "channels": 2}],
}
PROBE_VIDEO_ONLY = {"format": {"duration": "10"}, "streams": [{"codec_type": "video"}]}


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    media_file = tmp_path / "movie.mkv"
    media_file.write_bytes(b"")
    return media_file


def run_cli(media_file: Path, probe: dict, *args: str, run_process=None) -> tuple[int, list[list[str]]]:
    with (
        patch.object(sys, "argv", ["baycone", str(media_file), *args]),
        patch("baycone.cli.get_file_info", return_value=probe),
        patch("baycone.cli.run_process", side_effect=run_process) as mock_run,
    ):
        code = cli.main()
    return code, [call.args[0] for call in mock_run.call_args_list]


def maps(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]


class TestMain:
    def test_tags_the_mp4_language_code(self, media_file):
        code, cmds = run_cli(media_file, PROBE_STEREO, "-l", "cze")
        assert code == 0
        assert "language=ces" in cmds[-1]

    def test_transcodes_video_without_audio(self, media_file):
        code, cmds = run_cli(media_file, PROBE_VIDEO_ONLY)
        assert code == 0
        assert all(not m.startswith("0:a") for cmd in cmds for m in maps(cmd))

    def test_removes_temp_dir_when_transcoding_fails(self, media_file):
        def fail(cmd, *args):
            cli.get_file_passlog(media_file).write_text("")
            raise subprocess.CalledProcessError(1, cmd)

        code, _ = run_cli(media_file, PROBE_STEREO, run_process=fail)
        assert code == 2
        assert not cli.get_dir_temp(media_file).exists()
