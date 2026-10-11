import logging
from dataclasses import dataclass
from pathlib import Path

import pycountry
from charset_normalizer import from_bytes

from baycone.audio import normalize_language
from baycone.config import LocationConfig

log = logging.getLogger(__name__)

# Bitmap subtitles (PGS, VobSub, DVB) cannot be converted to SRT
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


@dataclass
class SubtitleStream:
    index: int  # Position among the subtitle streams only, used as `0:s:{index}`
    codec: str = ""
    lang: str = ""
    special: bool = False


def select_subtitle_streams(streams: list[SubtitleStream], location_config: LocationConfig) -> list[SubtitleStream]:
    """First text stream of each of `target_subtitle_languages`, in that order; languages without one are skipped."""
    candidates = [s for s in streams if s.codec in TEXT_SUBTITLE_CODECS and not s.special]
    languages = dict.fromkeys(normalize_language(lang) for lang in location_config.target_subtitle_languages)

    selected = []
    for language in languages:
        if stream := next((s for s in candidates if s.lang == language), None):
            selected.append(stream)
    return selected


def language_suffix(lang: str) -> str:
    """ISO 639-1 where the language has one, otherwise the ISO 639-2 code as is."""
    language = pycountry.languages.get(alpha_3=lang)
    return getattr(language, "alpha_2", lang)


def ensure_utf8(path: Path) -> None:
    """ffmpeg passes the bytes of text subtitles through, so a legacy codepage ends up in the file."""
    data = path.read_bytes()
    best = from_bytes(data).best()
    if best is None:
        log.warning(f"Unknown encoding of subtitles '{path}', left as is")
        return
    if best.encoding in ("ascii", "utf_8") and not best.bom:
        return
    path.write_text(str(best), encoding="utf-8")
    log.info(f"Subtitles '{path.name}' converted from {best.encoding} to UTF-8")
