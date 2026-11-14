"""Selection of text subtitles and their conversion to UTF-8."""

import logging
from dataclasses import dataclass
from pathlib import Path

from charset_normalizer import from_bytes

from baycone.audio import normalize_language
from baycone.settings import EncodingSettings
from baycone.languages import ALPHA_3_TO_ALPHA_2

log = logging.getLogger(__name__)

# Bitmap subtitles (PGS, VobSub, DVB) cannot be converted to SRT
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
SRT_CODECS = {"subrip", "srt"}


@dataclass
class SubtitleStream:
    """A probed subtitle stream."""

    index: int  # Position among the subtitle streams only, used as `0:s:{index}`
    codec: str = ""
    lang: str = ""
    special: bool = False


def select_subtitle_streams(streams: list[SubtitleStream], settings: EncodingSettings) -> list[SubtitleStream]:
    """
    Selects the subtitles to extract.

    :param streams: probed subtitle streams
    :param settings: `target_subtitle_languages` decides the selection
    :return: the first text stream of each language, in that order; languages without one are skipped
    """
    candidates = [s for s in streams if s.codec in TEXT_SUBTITLE_CODECS and not s.special]
    languages = dict.fromkeys(normalize_language(lang) for lang in settings.target_subtitle_languages)

    selected = []
    for language in languages:
        if stream := next((s for s in candidates if s.lang == language), None):
            selected.append(stream)
    return selected


def language_suffix(lang: str) -> str:
    """
    Gives the language code used in subtitle file names.

    :param lang: ISO 639-2/T code
    :return: the ISO 639-1 code where the language has one, otherwise `lang`
    """
    return ALPHA_3_TO_ALPHA_2.get(lang, lang)


def ensure_utf8(path: Path) -> None:
    """
    Converts subtitles to UTF-8 in place: ffmpeg passes text subtitles through in their legacy codepage.

    :param path: the subtitle file, left as is when its encoding is unknown
    """
    data = path.read_bytes()
    best = from_bytes(data).best()
    if best is None:
        log.warning(f"Unknown encoding of subtitles '{path}', left as is")
        return
    if best.encoding in ("ascii", "utf_8") and not best.bom:
        return
    path.write_text(str(best), encoding="utf-8")
    log.info(f"Subtitles '{path.name}' converted from {best.encoding} to UTF-8")
