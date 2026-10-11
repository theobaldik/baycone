import pytest

from baycone.config import LocationConfig
from baycone.subtitles import SubtitleStream, ensure_utf8, language_suffix, select_subtitle_streams


def stream(index: int, lang: str, codec: str = "subrip", **kwargs) -> SubtitleStream:
    return SubtitleStream(index=index, lang=lang, codec=codec, **kwargs)


def select(streams: list[SubtitleStream], languages: list[str]) -> list[int]:
    location_config = LocationConfig(target_subtitle_languages=languages)
    return [s.index for s in select_subtitle_streams(streams, location_config)]


class TestSelect:
    def test_disabled_by_default(self):
        assert select([stream(0, "eng")], []) == []

    def test_follows_the_configured_order(self):
        assert select([stream(0, "eng"), stream(1, "ces")], ["ces", "eng"]) == [1, 0]

    def test_one_stream_per_language(self):
        assert select([stream(0, "eng"), stream(1, "eng")], ["eng", "eng"]) == [0]

    def test_bibliographic_codes_are_normalized(self):
        assert select([stream(0, "ces")], ["cze"]) == [0]

    def test_missing_language_is_skipped(self):
        assert select([stream(0, "eng")], ["deu", "eng"]) == [0]

    @pytest.mark.parametrize("codec", ["hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"])
    def test_bitmap_subtitles_are_skipped(self, codec):
        assert select([stream(0, "eng", codec)], ["eng"]) == []

    def test_a_text_stream_wins_over_a_bitmap_one(self):
        assert select([stream(0, "eng", "hdmv_pgs_subtitle"), stream(1, "eng", "ass")], ["eng"]) == [1]

    def test_special_streams_are_skipped(self):
        assert select([stream(0, "eng", special=True), stream(1, "eng")], ["eng"]) == [1]


class TestLanguageSuffix:
    def test_uses_two_letters(self):
        assert language_suffix("ces") == "cs"
        assert language_suffix("eng") == "en"

    def test_keeps_three_letters_without_a_short_form(self):
        assert language_suffix("und") == "und"
        assert language_suffix("xyz") == "xyz"


class TestEnsureUtf8:
    TEXT = "1\n00:00:01,000 --> 00:00:02,000\nPříliš žluťoučký kůň úpěl ďábelské ódy\n"

    def test_utf8_is_left_untouched(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_bytes(self.TEXT.encode("utf-8"))
        ensure_utf8(path)
        assert path.read_bytes() == self.TEXT.encode("utf-8")

    def test_legacy_codepage_is_converted(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_bytes(self.TEXT.encode("cp1250"))
        ensure_utf8(path)
        assert path.read_text(encoding="utf-8") == self.TEXT

    def test_bom_is_dropped(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_bytes(self.TEXT.encode("utf-8-sig"))
        ensure_utf8(path)
        assert path.read_bytes() == self.TEXT.encode("utf-8")
