"""
Transcode videos into streaming-friendly MP4 with ffmpeg.

Encoder(EncodingSettings()).process(Job(input_file, output_file, work_dir))
"""

from baycone.encoding import Analyzer, Encoder, FileInfo, Job, ProgressCallback, VideoStream
from baycone.errors import (
    BayconeError,
    EncodingError,
    InsufficientSpaceError,
    InvalidCutError,
    InvalidFileError,
    SourceChangedError,
)
from baycone.settings import EncodingSettings, FFmpegTools

__all__ = [
    "Analyzer",
    "BayconeError",
    "Encoder",
    "EncodingError",
    "EncodingSettings",
    "FFmpegTools",
    "FileInfo",
    "InsufficientSpaceError",
    "InvalidCutError",
    "InvalidFileError",
    "Job",
    "ProgressCallback",
    "SourceChangedError",
    "VideoStream",
]
