import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from baycone.files import Signature, write_atomic

log = logging.getLogger(__name__)


@dataclass
class Attempt:
    """A file being processed; still recorded after a restart when the service died instead of finishing it."""

    signature: Signature
    attempts: int = 1


@dataclass
class State:
    """What a location has to remember across restarts; paths are relative to its root."""

    failed: dict[str, Signature] = field(default_factory=dict)
    in_progress: dict[str, Attempt] = field(default_factory=dict)

    @classmethod
    def load(cls, file_path: Path) -> State:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return cls(
                failed={path: tuple(failed) for path, failed in data["failed"].items()},
                in_progress=cls._load_in_progress(data),
            )
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning(f"State '{file_path}' is unreadable, starting with an empty one: {e}")
            return cls()

    @staticmethod
    def _load_in_progress(data: dict) -> dict[str, Attempt]:
        # Before 0.8.0 only one file was ever in progress, kept so an upgrade does not reset its count
        if "in_progress" not in data:
            if not data["current"] or not data["current_signature"]:
                return {}
            return {str(data["current"]): Attempt(tuple(data["current_signature"]), int(data["attempts"]))}
        return {
            path: Attempt(tuple(attempt["signature"]), int(attempt["attempts"]))
            for path, attempt in data["in_progress"].items()
        }

    def save(self, file_path: Path) -> None:
        """A state that cannot be saved is only lost on the next restart."""
        try:
            write_atomic(file_path, json.dumps(asdict(self)))
        except OSError as e:
            log.warning(f"State '{file_path}' could not be saved: {e}")
