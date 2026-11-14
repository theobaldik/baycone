"""Probed audio streams and the selection of output tracks."""

from dataclasses import dataclass

from baycone.settings import EncodingSettings
from baycone.languages import BIBLIOGRAPHIC_TO_TERMINOLOGY

UNDEFINED_LANGUAGE = "und"
# Stands for the original track's language in `target_audio_languages`
ORIGINAL_LANGUAGE = "original"

TRACK_NAMES: dict[int, str] = {
    1: "Mono",
    2: "Stereo",
    3: "Stereo 2.1",
    6: "Surround 5.1",
    8: "Surround 7.1",
    9: "Surround 7.2",
}

# Tracks with any of these ffprobe dispositions are never kept; 'dub' is not one of them, dubs are regular languages
SPECIAL_DISPOSITIONS: tuple[str, ...] = (
    "comment",
    "visual_impaired",
    "descriptions",
    "hearing_impaired",
    "clean_effects",
    "karaoke",
    "lyrics",
)


# aac bitrate needed for the same quality, per kbps of the source codec
AAC_EFFICIENCY: dict[str, float] = {"opus": 1.5, "vorbis": 1.1, "mp3": 0.75, "ac3": 0.65, "dts": 0.5}


def normalize_language(code: str) -> str:
    """
    Normalizes a language code to ISO 639-2/T.

    :param code: ISO 639-2/B or /T code, in any case
    :return: lower-case /T code (`cze` becomes `ces`), `und` for an empty one
    """
    code = code.strip().lower()
    # ISO 639-2/B and /T codes both occur in the wild, MP4 requires /T
    return BIBLIOGRAPHIC_TO_TERMINOLOGY.get(code, code) or UNDEFINED_LANGUAGE


def cap_bitrate(source: int, target: int) -> int:
    """
    Caps a bitrate without ever raising it.

    :param source: source bitrate, 0 when unknown
    :param target: the cap
    :return: the lower of the two, `target` when the source is unknown
    """
    return min(source, target) if source > 0 else target


@dataclass
class AudioStream:
    """A probed audio stream; `index` counts audio streams only, `channel_bitrate` is kbps, 0 when unknown."""

    index: int = 0
    shall_encode: bool = False
    codec: str = ""
    channel_bitrate: int = 0
    channels: int = 0
    lang: str = UNDEFINED_LANGUAGE
    title: str = ""
    original: bool = False
    special: bool = False

    @property
    def overall_bitrate(self) -> int:
        return self.channel_bitrate * self.channels

    @property
    def aac_channel_bitrate(self) -> int:
        """`channel_bitrate` converted to its AAC equivalent."""
        return int(self.channel_bitrate * AAC_EFFICIENCY.get(self.codec, 1.0))

    @property
    def aac_overall_bitrate(self) -> int:
        return self.aac_channel_bitrate * self.channels

    def generate_track_name(self) -> str:
        """
        Names the track by its channel layout.

        :return: `Stereo`, `Surround 5.1`, ..., empty for an unusual layout
        """
        return TRACK_NAMES.get(self.channels, "")


@dataclass
class AudioTrack:
    """An output audio track: `stream` copied, or encoded to AAC at `bitrate`."""

    stream: AudioStream
    bitrate: int = 0  # kbps, 0 copies the stream
    default: bool = False

    @property
    def title(self) -> str:
        return self.stream.generate_track_name()

    @property
    def disposition(self) -> str:
        """The ffmpeg `-disposition` value."""
        flags = [name for name, enabled in (("default", self.default), ("original", self.stream.original)) if enabled]
        return "+".join(flags) or "0"


def _pick_by_channels(versions: list[AudioStream], preferred: int) -> AudioStream:
    """The fewest channels at or above `preferred`, else the most below it; ties keep the source order."""
    at_least = [s for s in versions if s.channels >= preferred]
    if at_least:
        return min(at_least, key=lambda s: s.channels)
    return max(versions, key=lambda s: s.channels)


def select_audio_tracks(streams: list[AudioStream], settings: EncodingSettings) -> list[AudioTrack]:
    """
    Selects the output audio tracks.

    :param streams: probed audio streams
    :param settings: `target_audio_*` decide the selection
    :return: one track per language of `target_audio_languages` found, in that order; the original alone when none
        is found
    """
    candidates = [s for s in streams if not s.special]

    if candidates:
        original = next((s for s in candidates if s.original), candidates[0])
        languages = [
            original.lang if lang.strip().lower() == ORIGINAL_LANGUAGE else normalize_language(lang)
            for lang in settings.target_audio_languages
        ]
        selected = []
        for language in dict.fromkeys(languages):
            versions = [s for s in candidates if s.lang == language]
            if versions:
                selected.append(_pick_by_channels(versions, settings.target_audio_channels))
        selected = selected or [original]
    else:
        selected = streams[:1]

    return [
        AudioTrack(
            stream=stream,
            bitrate=(
                cap_bitrate(stream.aac_overall_bitrate, settings.target_audio_channel_bitrate * stream.channels)
                if stream.shall_encode
                else 0
            ),
            default=i == 0,
        )
        for i, stream in enumerate(selected)
    ]
