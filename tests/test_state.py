import json
from pathlib import Path
from unittest.mock import patch

import pytest

from baycone.state import State


@pytest.fixture
def state_file(tmp_path) -> Path:
    return tmp_path / "state.json"


class TestState:
    def test_missing_file_is_an_empty_state(self, state_file):
        assert State.load(state_file) == State()

    def test_roundtrip(self, state_file):
        state = State(
            failed={"Show/episode.mkv": (1000, 1_700_000_000_000_000_000)},
            current="movie.mkv",
            current_signature=(2000, 1_700_000_000_000_000_001),
            attempts=2,
        )
        state.save(state_file)
        assert State.load(state_file) == state

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
        State(attempts=1).save(state_file)
        with patch("baycone.files.Path.replace", side_effect=OSError("power loss")):
            State(attempts=2).save(state_file)
        assert json.loads(state_file.read_text())["attempts"] == 1

    def test_failed_save_only_warns(self, tmp_path, caplog):
        State().save(tmp_path / "missing" / "state.json")
        assert "could not be saved" in caplog.text
