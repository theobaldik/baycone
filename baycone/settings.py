import logging
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields

log = logging.getLogger(__name__)


def _validate(key: str, value: object, default: object) -> None:
    """A value has to be of its default's kind; a wrong one would only blow up later, mid-run."""
    if isinstance(default, bool):
        valid = isinstance(value, bool)
    elif isinstance(default, (int, float)):
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    elif isinstance(default, list):
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    else:
        valid = isinstance(value, type(default))

    if not valid:
        raise ValueError(f"Invalid value {value!r} for configuration key '{key}'")


class Serializable:

    def from_dict(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError(f"Expected a mapping of configuration keys, got {data!r}")

        names = {f.name for f in fields(self)}
        for key, value in data.items():
            if key in names:
                _validate(key, value, getattr(self, key))
                setattr(self, key, deepcopy(value))
            else:
                log.warning(f"Unknown configuration key '{key}' ignored")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


@dataclass
class EncodingSettings(Serializable):
    allowed_containers: list[str] = field(default_factory=lambda: ["mp4"])
    allowed_video_codecs: list[str] = field(default_factory=lambda: ["h264"])
    allowed_audio_codecs: list[str] = field(default_factory=lambda: ["aac"])

    target_extension: str = ".mp4"
    target_container: str = "mp4"

    target_video_codec: str = "h264"
    target_video_bitrate: int = 2250

    target_audio_codec: str = "aac"
    target_audio_channel_bitrate: int = 64
    target_audio_languages: list[str] = field(default_factory=lambda: ["original", "eng", "cze"])
    target_audio_channels: int = 2

    # Text subtitles of these languages are extracted to `<name>.<lang>.srt` next to the output file
    target_subtitle_languages: list[str] = field(default_factory=list)

    limit_video_bitrate: int = 2800
    limit_video_height: int = 1080
    limit_audio_channel_bitrate: int = 96

    # Turned off for filesystems that report a free space that is not there to use
    check_free_space: bool = True

    @property
    def limit_video_width(self) -> int:
        """The width of a 16:9 box, so a wide film is not scaled past it by its height alone."""
        return self.limit_video_height * 16 // 9 // 2 * 2
