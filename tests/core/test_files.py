import os
from pathlib import Path
from unittest.mock import patch

from baycone.files import file_signature, fsync_dir, write_atomic


class TestFileSignature:
    def test_missing_file_has_none(self, tmp_path):
        assert file_signature(tmp_path / "missing.mkv") is None

    def test_changes_with_the_size(self, tmp_path):
        video = tmp_path / "movie.mkv"
        video.write_bytes(b"a")
        before = file_signature(video)
        video.write_bytes(b"ab")
        assert file_signature(video) != before

    def test_changes_with_the_mtime(self, tmp_path):
        video = tmp_path / "movie.mkv"
        video.touch()
        before = file_signature(video)
        os.utime(video, ns=(before[1], before[1] + 1_000_000_000))
        assert file_signature(video) != before

    def test_stable_for_an_untouched_file(self, tmp_path):
        video = tmp_path / "movie.mkv"
        video.touch()
        assert file_signature(video) == file_signature(video)


class TestWriteAtomic:
    def test_writes_the_content(self, tmp_path):
        write_atomic(tmp_path / "state.json", "content")
        assert (tmp_path / "state.json").read_text() == "content"

    def test_leaves_no_temp_file_behind(self, tmp_path):
        write_atomic(tmp_path / "state.json", "content")
        assert [path.name for path in tmp_path.iterdir()] == ["state.json"]

    def test_content_is_synced_before_it_replaces_the_file(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("previous")
        seen = []
        real_fsync = os.fsync

        def recording_fsync(fd):
            seen.append(target.read_text())
            real_fsync(fd)

        with patch("baycone.files.os.fsync", side_effect=recording_fsync):
            write_atomic(target, "new")
        # File first, then its directory
        assert seen == ["previous", "new"]


class TestFsyncDir:
    def test_filesystem_that_cannot_sync_a_directory_is_tolerated(self, tmp_path):
        with patch("baycone.files.os.fsync", side_effect=OSError("invalid argument")):
            fsync_dir(tmp_path)

    def test_missing_directory_is_tolerated(self, tmp_path):
        fsync_dir(Path(tmp_path / "missing"))
