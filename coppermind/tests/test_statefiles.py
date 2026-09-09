from pathlib import Path

import pytest

from coppermind.schema import default_schema
from coppermind.settings import default_settings
from coppermind.statefiles import RevisionConflict, StateStore


def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state")


def test_a_fresh_install_gets_working_defaults_at_revision_one(tmp_path: Path):
    state = store(tmp_path)
    written = state.ensure("settings", default_settings().model_dump(mode="json"))
    assert written.revision == 1
    assert state.read("settings").body["notes"]["review_folder"] == "Review"


def test_ensure_never_overwrites_what_an_operator_changed(tmp_path: Path):
    state = store(tmp_path)
    state.ensure("settings", default_settings().model_dump(mode="json"))
    body = state.read("settings").body
    body["notes"]["review_folder"] = "Inbox"
    state.write("settings", body, if_revision=1)

    state.ensure("settings", default_settings().model_dump(mode="json"))
    assert state.read("settings").body["notes"]["review_folder"] == "Inbox"


def test_each_write_bumps_the_revision(tmp_path: Path):
    state = store(tmp_path)
    state.ensure("schema", default_schema().model_dump(mode="json"))
    second = state.write("schema", state.read("schema").body, if_revision=1)
    assert second.revision == 2


def test_a_stale_write_is_refused_rather_than_silently_winning(tmp_path: Path):
    state = store(tmp_path)
    state.ensure("settings", default_settings().model_dump(mode="json"))
    state.write("settings", state.read("settings").body, if_revision=1)
    with pytest.raises(RevisionConflict) as raised:
        state.write("settings", state.read("settings").body, if_revision=1)
    assert raised.value.current_revision == 2


def test_hash_bearing_files_are_not_world_readable(tmp_path: Path):
    state = store(tmp_path)
    state.ensure("keys", {"keys": []})
    assert state.path_for("keys").stat().st_mode & 0o077 == 0


def test_an_unknown_state_file_is_refused(tmp_path: Path):
    with pytest.raises(ValueError):
        store(tmp_path).path_for("something-else")
