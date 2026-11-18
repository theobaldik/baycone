"""One file processed in a child process of the service, so a stop can interrupt it like a single-threaded run."""

import logging
import os
import signal
import sys
from enum import IntEnum
from types import FrameType
from typing import NoReturn

from baycone.encoding import Encoder
from baycone.errors import BayconeError, InsufficientSpaceError
from baycone.settings import FFmpegTools
from baycone_service.layout import LocatedFile, Slot, archive_original
from baycone_service.logger import setup_logging

log = logging.getLogger(__name__)


class JobResult(IntEnum):
    """The child's exit code; any other one means it died, e.g. killed by the OOM killer."""

    DONE = 0
    # Clear of 1 and 2, which Python exits with on an uncaught exception or a bad command line
    FAILED = 10
    NO_SPACE = 11
    STOPPED = 12


def _interrupt(signum: int, frame: FrameType | None) -> None:
    # A repeated signal must not interrupt the cleanup the first one started
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


def process_file(located: LocatedFile, tools: FFmpegTools, slot: Slot) -> JobResult:
    """The original is archived only once its output is published."""
    encoder = Encoder(located.location_config, tools)
    try:
        encoder.process(located.job(slot))
    except InsufficientSpaceError as e:
        # Not the file's fault: it is left alone and tried again later
        log.warning(f"Not enough free space: {e}")
        return JobResult.NO_SPACE
    except BayconeError as e:
        log.error(f"Processing aborted: {e}")
        return JobResult.FAILED
    archive_original(located)
    return JobResult.DONE


def run_job(located: LocatedFile, tools: FFmpegTools, slot: Slot) -> NoReturn:
    """The target of the child process, its exit code is a `JobResult`."""
    setup_logging(slot)
    # A group of its own, which the dispatcher kills as a whole when the job dies: its ffmpeg would outlive it
    if hasattr(os, "setpgrp"):
        os.setpgrp()
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        result = process_file(located, tools, slot)
    except KeyboardInterrupt:
        log.info(f"Processing of '{located.file_path}' stopped")
        result = JobResult.STOPPED
    except Exception:
        log.exception(f"Processing failed for '{located.file_path}'")
        result = JobResult.FAILED
    sys.exit(result)
