"""The helper reads its own settings from the store's file and nothing else."""

from dataclasses import asdict
from pathlib import Path

import pytest
from coppermind_git.settings import HelperSettings, SettingsError, load

from coppermind.settings import GitSettings, NotesSettings, default_settings
from coppermind.statefiles import StateStore


def test_the_helper_ships_the_same_defaults_as_the_settings_model():
    ours = asdict(HelperSettings())
    shipped = GitSettings().model_dump()
    # Every key of the shared git section must be one the helper honours.
    assert {key: ours[key] for key in shipped} == shipped
    assert ours["sources_folder"] == NotesSettings().sources_folder
    assert ours["trash_folder"] == NotesSettings().trash_folder


def test_no_settings_file_means_the_shipped_defaults(tmp_path: Path):
    assert load(tmp_path / "settings.yaml") == HelperSettings()


def test_it_reads_the_file_bootstrap_writes_and_follows_a_change(tmp_path: Path):
    state = StateStore(tmp_path)
    written = state.write("settings", default_settings().model_dump(mode="json"), if_revision=None)
    assert load(state.path_for("settings")) == HelperSettings()

    body = dict(written.body)
    body["git"] = {**body["git"], "debounce_s": 5, "identity_name": "Notes Bot"}
    body["notes"] = {**body["notes"], "trash_folder": "Deleted"}
    body["a_section_from_a_newer_store"] = {"anything": True}
    state.write("settings", body, if_revision=written.revision)

    loaded = load(state.path_for("settings"))
    assert (loaded.debounce_s, loaded.identity_name, loaded.trash_folder) == (
        5,
        "Notes Bot",
        "Deleted",
    )


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("git:\n  debounce_s: 0\n", "git.debounce_s"),
        ("git:\n  poll_interval_s: true\n", "git.poll_interval_s"),
        ("git:\n  poll_interval_s: '300'\n", "git.poll_interval_s"),
        ("git:\n  enabled: 'yes'\n", "git.enabled"),
        ("git:\n  identity_name: '  '\n", "git.identity_name"),
        ("notes:\n  trash_folder: [a]\n", "notes.trash_folder"),
        ("notes:\n  trash_folder: .\n", "notes.trash_folder"),
        ("notes:\n  sources_folder: /\n", "notes.sources_folder"),
        ("notes:\n  sources_folder: a/../..\n", "notes.sources_folder"),
        ("git: [enabled]\n", "git"),
        ("- a list\n", "not a mapping"),
        ("git: {enabled\n", "not valid YAML"),
    ],
)
def test_an_unusable_value_is_named(tmp_path: Path, text: str, names: str):
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsError, match=names):
        load(path)


def test_a_file_that_cannot_be_read_or_decoded_is_a_settings_error(tmp_path: Path):
    latin1 = tmp_path / "settings.yaml"
    latin1.write_bytes("git:\n  identity_name: Zoë\n".encode("latin-1"))
    with pytest.raises(SettingsError, match="could not be read"):
        load(latin1)

    unreadable = tmp_path / "directory" / "settings.yaml"
    unreadable.mkdir(parents=True)
    with pytest.raises(SettingsError, match="could not be read"):
        load(unreadable)
