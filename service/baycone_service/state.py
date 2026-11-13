import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from baycone.files import Signature, write_atomic

log = logging.getLogger(__name__)


@dataclass
class State:
    """What a location has to remember across restarts; paths are relative to its root."""

    failed: dict[str, Signature] = field(default_factory=dict)

    # The file being processed; it is still set after a restart when the service died instead of finishing it
    current: str = ""
    current_signature: Signature | None = None
    attempts: int = 0

    @classmethod
    def load(cls, file_path: Path) -> State:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            signature = data["current_signature"]
            return cls(
                failed={path: tuple(failed) for path, failed in data["failed"].items()},
                current=str(data["current"]),
                current_signature=tuple(signature) if signature else None,
                attempts=int(data["attempts"]),
            )
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning(f"State '{file_path}' is unreadable, starting with an empty one: {e}")
            return cls()

    def save(self, file_path: Path) -> None:
        """A state that cannot be saved is only lost on the next restart."""
        try:
            write_atomic(file_path, json.dumps(asdict(self)))
        except OSError as e:
            log.warning(f"State '{file_path}' could not be saved: {e}")
