"""Errors raised by `Encoder.process()`."""


class BayconeError(Exception):
    """A file that could not be processed; the input file is left untouched."""


class InsufficientSpaceError(BayconeError):
    """Raised before anything is written, so the file can simply be tried again later."""


class InvalidFileError(BayconeError):
    """Unreadable, truncated or otherwise not a media file."""


class InvalidCutError(BayconeError):
    """`Job.start` / `Job.end` do not lie within the file, raised before anything is written."""


class EncodingError(BayconeError):
    """FFmpeg failed, or the settings ask for something it cannot do."""


class SourceChangedError(BayconeError):
    """The input changed or disappeared while it was processed, so the result was discarded."""
