from pathlib import Path
import pytest

from baycone_service.config import SpecialDirs
from baycone_service.layout import LocatedFile, Slot, archive_original


@pytest.fixture
def special_dirs(location_config) -> SpecialDirs:
    dirs = SpecialDirs(location_config.abspath)
    dirs.create()
    return dirs


@pytest.fixture
def media_file(location_dir) -> Path:
    video = location_dir / "movie.mp4"
    video.touch()
    return video


def media_in(location_dir: Path, relative: str) -> Path:
    video = location_dir / relative
    video.parent.mkdir(parents=True, exist_ok=True)
    video.touch()
    return video


class TestLocatedFile:
    def test_resolves_the_location(self, media_file, location_config, location_dir):
        located = LocatedFile.resolve(media_file)
        assert located.location_config is location_config
        assert located.special_dirs.root_dir == location_dir

    def test_rejects_a_path_outside_any_location(self, location_config, tmp_path):
        with pytest.raises(ValueError):
            LocatedFile.resolve(tmp_path.parent / "elsewhere" / "movie.mp4")

    def test_relative_path_in_subdirectory(self, location_dir):
        video = media_in(location_dir, "ShowName/episode.mkv")
        assert LocatedFile.resolve(video).relative_path == Path("ShowName/episode.mkv")

    def test_original_path_keeps_the_source_extension(self, location_dir, special_dirs):
        video = media_in(location_dir, "movie.mkv")
        assert LocatedFile.resolve(video).original_path == special_dirs.originals_dir / "movie.mkv"

    def test_processed_path_uses_the_target_extension(self, location_dir, special_dirs):
        video = media_in(location_dir, "Show/episode.mkv")
        assert LocatedFile.resolve(video).processed_path == special_dirs.processed_dir / "Show" / "episode.mp4"

    def test_job_works_in_the_location_temp_dir(self, location_dir, special_dirs):
        video = media_in(location_dir, "Show/episode.mkv")
        job = LocatedFile.resolve(video).job()
        assert job.input_file == video
        assert job.output_file == special_dirs.processed_dir / "Show" / "episode.mp4"
        assert job.work_dir == special_dirs.temp_dir
        assert job.name == "Show/episode.mkv"

    def test_job_uses_the_service_work_dirs(self, media_file, special_dirs):
        job = LocatedFile.resolve(media_file).job()
        assert (job.passlog_dir, job.encoded_dir, job.optimized_dir) == (
            special_dirs.passlog_dir,
            special_dirs.encoded_dir,
            special_dirs.optimized_dir,
        )

    def test_copy_slot_has_a_work_dir_of_its_own(self, media_file, special_dirs):
        """Its encoded and optimized files must not mix with those of the encode running alongside."""
        encode, copy = LocatedFile.resolve(media_file).job(Slot.ENCODE), LocatedFile.resolve(media_file).job(Slot.COPY)
        assert copy.work_dir == special_dirs.copy_dir
        assert {copy.encoded_dir, copy.optimized_dir}.isdisjoint({encode.encoded_dir, encode.optimized_dir})
        assert copy.output_file == encode.output_file


class TestArchiveOriginal:
    def test_moves_original_to_originals_dir(self, media_file, special_dirs):
        archive_original(LocatedFile.resolve(media_file))
        assert not media_file.exists()
        assert (special_dirs.originals_dir / "movie.mp4").is_file()

    def test_never_replaces_an_earlier_original(self, media_file, special_dirs):
        earlier = special_dirs.originals_dir / "movie.mp4"
        earlier.write_text("first")
        (special_dirs.originals_dir / "movie.1.mp4").write_text("second")
        media_file.write_text("third")

        archive_original(LocatedFile.resolve(media_file))

        assert earlier.read_text() == "first"
        assert (special_dirs.originals_dir / "movie.1.mp4").read_text() == "second"
        assert (special_dirs.originals_dir / "movie.2.mp4").read_text() == "third"

    def test_keeps_subdirectory_structure(self, location_dir, special_dirs):
        video = media_in(location_dir, "ShowName/Season 1/episode.mkv")
        archive_original(LocatedFile.resolve(video))
        assert (special_dirs.originals_dir / "ShowName" / "Season 1" / "episode.mkv").is_file()

    def test_removes_directories_left_empty(self, location_dir, special_dirs):
        video = media_in(location_dir, "ShowName/Season 1/episode.mkv")
        archive_original(LocatedFile.resolve(video))
        assert not (location_dir / "ShowName").exists()

    def test_keeps_directories_holding_unprocessed_files(self, location_dir, special_dirs):
        """Regression: cleanup used to rmtree the whole top-level subdirectory, deleting pending files."""
        processed = media_in(location_dir, "ShowName/Season 1/episode01.mkv")
        pending = media_in(location_dir, "ShowName/Season 1/episode02.mkv")

        archive_original(LocatedFile.resolve(processed))

        assert pending.is_file()
        assert pending.parent.is_dir()

    def test_keeps_sibling_directories(self, location_dir, special_dirs):
        processed = media_in(location_dir, "ShowName/Season 1/episode01.mkv")
        pending = media_in(location_dir, "ShowName/Season 2/episode01.mkv")

        archive_original(LocatedFile.resolve(processed))

        assert pending.is_file()
        assert not processed.parent.exists()
        assert (location_dir / "ShowName").is_dir()

    def test_never_removes_the_location_root(self, media_file, location_dir, special_dirs):
        archive_original(LocatedFile.resolve(media_file))
        assert location_dir.is_dir()
