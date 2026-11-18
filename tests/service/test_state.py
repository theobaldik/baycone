import json
from pathlib import Path
from unittest.mock import patch

import pytest

from baycone_service.state import Attempt, State


@pytest.fixture
def state_file(tmp_path) -> Path:
    return tmp_path / "state.json"


class TestState:
    def test_missing_file_is_an_empty_state(self, state_file):
        assert State.load(state_file) == State()

    def test_roundtrip(self, state_file):
        state = State(
            failed={"Show/episode.mkv": (1000, 1_700_000_000_000_000_000)},
            in_progress={
                "movie.mkv": Attempt((2000, 1_700_000_000_000_000_001), attempts=2),
                "Show/other.mkv": Attempt((3000, 1_700_000_000_000_000_002)),
            },
        )
        state.save(state_file)
        assert State.load(state_file) == state

    def test_file_in_progress_before_0_8_keeps_its_attempts(self, state_file):
        """One file at a time used to be recorded on its own; an upgrade must not reset its count."""
        state_file.write_text(
            json.dumps({"failed": {}, "current": "movie.mkv", "current_signature": [2000, 7], "attempts": 2})
        )
        assert State.load(state_file) == State(in_progress={"movie.mkv": Attempt((2000, 7), attempts=2)})

    def test_nothing_in_progress_before_0_8(self, state_file):
        state_file.write_text(
            json.dumps({"failed": {"a.mkv": [1, 2]}, "current": "", "current_signature": None, "attempts": 0})
        )
        assert State.load(state_file) == State(failed={"a.mkv": (1, 2)})

    def test_roundtrip_without_a_current_file(self, state_file):
        State(failed={"movie.mkv": (1, 2)}).save(state_file)
        assert State.load(state_file) == State(failed={"movie.mkv": (1, 2)})

    @pytest.mark.parametrize(
        "content",
        ["", "{ not json", "[]", "{}", '{"failed": [], "current": "", "current_signature": null, "attempts": 0}'],
        ids=["empty", "broken", "list", "no keys", "wrong type"],
    )
    def test_unreadable_file_is_an_empty_state(self, state_file, content, caplog):
        state_file.write_text(content)
        assert State.load(state_file) == State()
        assert "unreadable" in caplog.text

    def test_save_replaces_the_file_in_one_step(self, state_file):
        """A power loss mid-save must leave the previous state, not half of the new one."""
        State(failed={"a.mkv": (1, 2)}).save(state_file)
        with patch("baycone.files.Path.replace", side_effect=OSError("power loss")):
            State(failed={"b.mkv": (3, 4)}).save(state_file)
        assert list(json.loads(state_file.read_text())["failed"]) == ["a.mkv"]

    def test_failed_save_only_warns(self, tmp_path, caplog):
        State().save(tmp_path / "missing" / "state.json")
        assert "could not be saved" in caplog.text
