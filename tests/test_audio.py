import pytest

from baycone.audio import AudioStream, AudioTrack, cap_bitrate, normalize_language, select_audio_tracks
from baycone.config import LocationConfig


def stream(index: int, lang: str, channels: int = 2, **kwargs) -> AudioStream:
    return AudioStream(index=index, lang=lang, channels=channels, codec="aac", **kwargs)


def select(streams: list[AudioStream], languages: list[str] | None = None, channels: int = 2) -> list[AudioStream]:
    location_config = LocationConfig(
        target_audio_languages=["eng", "cze"] if languages is None else languages,
        target_audio_channels=channels,
    )
    return [track.stream for track in select_audio_tracks(streams, location_config)]


class TestAudioStream:
    @pytest.mark.parametrize(
        "channels,expected",
        [
            (1, "Mono"),
            (2, "Stereo"),
            (3, "Stereo 2.1"),
            (6, "Surround 5.1"),
            (8, "Surround 7.1"),
            (9, "Surround 7.2"),
            (0, ""),
            (99, ""),
        ],
    )
    def test_generate_track_name(self, channels, expected):
        assert AudioStream(channels=channels).generate_track_name() == expected

    def test_overall_bitrate_is_per_channel_times_channels(self):
        assert AudioStream(channel_bitrate=64, channels=6).overall_bitrate == 384


class TestNormalizeLanguage:
    @pytest.mark.parametrize("code,expected", [("cze", "ces"), ("ces", "ces"), ("GER", "deu"), ("eng", "eng")])
    def test_bibliographic_codes_become_terminology_codes(self, code, expected):
        assert normalize_language(code) == expected

    @pytest.mark.parametrize("code", ["", "  "])
    def test_empty_is_undefined(self, code):
        assert normalize_language(code) == "und"


class TestCapBitrate:
    def test_never_upscales(self):
        assert cap_bitrate(96, 128) == 96

    def test_caps_at_target(self):
        assert cap_bitrate(640, 128) == 128

    def test_unknown_source_falls_back_to_target(self):
        assert cap_bitrate(0, 128) == 128


class TestAudioTrack:
    def test_title_is_the_channel_layout(self):
        assert AudioTrack(stream(0, "eng", channels=6)).title == "Surround 5.1"

    @pytest.mark.parametrize(
        "default,original,expected",
        [(True, True, "default+original"), (True, False, "default"), (False, True, "original"), (False, False, "0")],
    )
    def test_disposition(self, default, original, expected):
        assert AudioTrack(stream(0, "eng", original=original), default=default).disposition == expected


class TestLanguageSelection:
    def test_original_comes_first_regardless_of_preference(self):
        eng, ces = stream(0, "eng"), stream(1, "ces")
        assert select([eng, ces], languages=["cze", "eng"]) == [eng, ces]

    def test_preferred_languages_follow_in_config_order(self):
        jpn, eng, ces = stream(0, "jpn"), stream(1, "eng"), stream(2, "ces")
        assert select([jpn, eng, ces], languages=["cze", "eng"]) == [jpn, ces, eng]

    def test_original_flag_beats_position(self):
        ces, eng = stream(0, "ces"), stream(1, "eng", original=True)
        assert select([ces, eng], languages=["cze"]) == [eng, ces]

    def test_other_languages_are_dropped(self):
        eng, ger, ces = stream(0, "eng"), stream(1, "deu"), stream(2, "ces")
        assert select([eng, ger, ces]) == [eng, ces]

    def test_missing_preferred_languages_are_skipped(self):
        eng = stream(0, "eng")
        assert select([eng], languages=["cze", "fre"]) == [eng]

    def test_bibliographic_codes_in_config_match(self):
        eng, ces = stream(0, "eng"), stream(1, "ces")
        assert select([eng, ces], languages=["cze"]) == [eng, ces]

    def test_no_preferred_languages_keeps_only_the_original(self):
        eng, ces = stream(0, "eng"), stream(1, "ces")
        assert select([eng, ces], languages=[]) == [eng]

    def test_undefined_original_language_is_kept(self):
        und, eng = stream(0, "und"), stream(1, "eng")
        assert select([und, eng]) == [und, eng]


class TestSpecialTracks:
    def test_special_tracks_are_dropped(self):
        eng, commentary = stream(0, "eng"), stream(1, "eng", special=True)
        assert select([eng, commentary]) == [eng]

    def test_special_track_is_never_the_original(self):
        description, ces, eng = stream(0, "eng", special=True), stream(1, "ces"), stream(2, "eng")
        assert select([description, ces, eng]) == [ces, eng]

    def test_special_track_is_never_picked_for_its_channels(self):
        commentary, eng = stream(0, "eng", channels=2, special=True), stream(1, "eng", channels=6)
        assert select([commentary, eng], channels=2) == [eng]

    def test_only_special_tracks_fall_back_to_the_first_stream(self):
        first, second = stream(0, "eng", special=True), stream(1, "ces", special=True)
        assert select([first, second]) == [first]

    def test_no_streams_select_nothing(self):
        assert select([]) == []


class TestChannelSelection:
    def test_preferred_channel_count_wins(self):
        surround, stereo = stream(0, "eng", channels=6), stream(1, "eng", channels=2)
        assert select([surround, stereo], channels=2) == [stereo]

    def test_fewest_channels_above_the_preference(self):
        s71, s51, mono = stream(0, "eng", channels=8), stream(1, "eng", channels=6), stream(2, "eng", channels=1)
        assert select([s71, s51, mono], channels=2) == [s51]

    def test_most_channels_when_all_are_below_the_preference(self):
        mono, stereo = stream(0, "eng", channels=1), stream(1, "eng", channels=2)
        assert select([mono, stereo], channels=6) == [stereo]

    def test_ties_keep_the_source_order(self):
        first, second = stream(0, "eng"), stream(1, "eng")
        assert select([first, second]) == [first]

    def test_applies_to_every_language(self):
        eng = stream(0, "eng", channels=6)
        ces_surround, ces_stereo = stream(1, "ces", channels=6), stream(2, "ces", channels=2)
        assert select([eng, ces_surround, ces_stereo], channels=2) == [eng, ces_stereo]


class TestTrackOutput:
    def test_only_the_first_track_is_default(self):
        tracks = select_audio_tracks([stream(0, "eng"), stream(1, "ces")], LocationConfig())
        assert [t.default for t in tracks] == [True, False]

    def test_tracks_within_limits_are_copied(self):
        tracks = select_audio_tracks([stream(0, "eng", channel_bitrate=96)], LocationConfig())
        assert tracks[0].bitrate == 0

    def test_bitrate_is_per_channel_times_channels(self):
        location_config = LocationConfig(target_audio_channel_bitrate=64, target_audio_channels=6)
        source = stream(0, "eng", channels=6, channel_bitrate=128, shall_encode=True)
        assert select_audio_tracks([source], location_config)[0].bitrate == 384

    def test_unknown_source_bitrate_falls_back_to_target(self):
        location_config = LocationConfig(target_audio_channel_bitrate=64)
        source = stream(0, "eng", channels=2, shall_encode=True)
        assert select_audio_tracks([source], location_config)[0].bitrate == 128
