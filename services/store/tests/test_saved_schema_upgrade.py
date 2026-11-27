"""A saved schema moves to the current contract only where it still equals what shipped."""

import pytest
from coppermind_store.control import ControlState

from coppermind.schema import KeyDefinition, default_schema, shipped_key_upgrades

UPGRADES = shipped_key_upgrades()
TIME_FORMS = UPGRADES["time"][0]


def save(control: ControlState, definitions: list[KeyDefinition]) -> int:
    state = control.store.read("schema")
    body = dict(state.body)
    names = {definition.name for definition in definitions}
    body["keys"] = [key for key in body["keys"] if key["name"] not in names] + [
        definition.model_dump(mode="json") for definition in definitions
    ]
    return control.store.write("schema", body, if_revision=state.revision).revision


def saved_keys(control: ControlState) -> dict[str, KeyDefinition]:
    return {key.name: key for key in control.schema().keys}


def earlier(*, time_form: int) -> list[KeyDefinition]:
    return [
        UPGRADES["account"][0][0],
        UPGRADES["date"][0][0],
        TIME_FORMS[time_form],
        UPGRADES["timezone"][0][0],
    ]


@pytest.mark.parametrize("time_form", [0, 1])
def test_untouched_shipped_keys_move_to_the_current_contract(tmp_path, time_form):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    save(control, earlier(time_form=time_form))
    assert saved_keys(control)["account"].kind == "string"

    control.ensure_defaults()

    keys = saved_keys(control)
    shipped = {key.name: key for key in default_schema().keys}
    assert keys["account"] == shipped["account"]
    assert keys["date"] == shipped["date"]
    assert keys["time"] == UPGRADES["time"][1]
    assert "legacy" in keys["time"].description.lower()
    assert "timezone" not in keys
    revision = control.store.read("schema").revision
    control.ensure_defaults()
    assert control.store.read("schema").revision == revision


def test_customised_definitions_are_left_untouched(tmp_path):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    custom = [
        definition.model_copy(update={"description": "Our own wording."})
        for definition in earlier(time_form=1)
    ]
    revision = save(control, custom)

    control.ensure_defaults()

    keys = saved_keys(control)
    for definition in custom:
        assert keys[definition.name] == definition
    assert control.store.read("schema").revision == revision


def test_the_closed_shipped_type_vocabulary_opens_and_a_customised_one_stays(tmp_path):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    closed = UPGRADES["type"][0][0]
    save(control, [closed])
    assert saved_keys(control)["type"].kind == "enum"

    control.ensure_defaults()

    assert saved_keys(control)["type"] == UPGRADES["type"][1]
    assert saved_keys(control)["type"].kind == "string"

    custom = closed.model_copy(update={"vocabulary": ["work", "play"]})
    revision = save(control, [custom])
    control.ensure_defaults()
    assert saved_keys(control)["type"] == custom
    assert control.store.read("schema").revision == revision


def test_a_current_schema_is_not_rewritten(tmp_path):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    revision = control.store.read("schema").revision
    control.ensure_defaults()
    assert control.store.read("schema").revision == revision


def test_a_saved_schema_gains_the_artifact_kind_keys_roles_and_reference_tag_once(tmp_path):
    """Schema files written before artifacts existed get what the kind needs, and nothing else."""
    from coppermind.schema import OPTIONAL_ROLES, REFERENCE_TAG

    control = ControlState(tmp_path)
    control.ensure_defaults()
    state = control.store.read("schema")
    body = dict(state.body)
    body["keys"] = [key for key in body["keys"] if key["name"] not in {"kind", "artifacts"}]
    body["roles"] = {k: v for k, v in body["roles"].items() if k not in OPTIONAL_ROLES}
    body["tags"] = {"work": "The day job."}
    control.store.write("schema", body, if_revision=state.revision)

    control.ensure_defaults()

    schema = control.schema()
    keys = saved_keys(control)
    assert keys["kind"].vocabulary == ["note", "artifact"]
    assert keys["artifacts"].kind == "list"
    assert schema.roles["kind_key"] == "kind"
    assert schema.roles["artifacts_key"] == "artifacts"
    assert schema.tags == {"work": "The day job.", REFERENCE_TAG: schema.tags[REFERENCE_TAG]}
    revision = control.store.read("schema").revision
    control.ensure_defaults()
    assert control.store.read("schema").revision == revision


def test_an_operator_who_renamed_the_citation_key_keeps_it(tmp_path):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    state = control.store.read("schema")
    body = dict(state.body)
    for key in body["keys"]:
        if key["name"] == "artifacts":
            key["name"] = "cites"
    body["roles"]["artifacts_key"] = "cites"
    control.store.write("schema", body, if_revision=state.revision)

    control.ensure_defaults()

    keys = saved_keys(control)
    assert "cites" in keys and "artifacts" not in keys
    assert control.schema().optional_role("artifacts_key") == "cites"


@pytest.mark.parametrize("form", [0, 1])
def test_a_saved_context_that_held_notes_back_stops_doing_so(tmp_path, form):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    save(control, [UPGRADES["context"][0][form]])
    held = saved_keys(control)["context"]
    assert held.required is True or held.required_when == "type"

    control.ensure_defaults()

    shipped = {k.name: k for k in default_schema().keys}["context"]
    assert saved_keys(control)["context"] == shipped
    assert shipped.required is False and shipped.required_when is None


def test_a_saved_account_that_needed_a_customer_note_to_name_one_stops_doing_so(tmp_path):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    needed = UPGRADES["account"][0][1]
    assert needed.kind == "list" and needed.required_when == "context"
    save(control, [needed])

    control.ensure_defaults()

    shipped = {k.name: k for k in default_schema().keys}["account"]
    assert saved_keys(control)["account"] == shipped
    assert shipped.required_when is None
    revision = control.store.read("schema").revision
    control.ensure_defaults()
    assert control.store.read("schema").revision == revision


def test_a_context_rule_an_operator_wrote_is_left_alone(tmp_path):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    mine = UPGRADES["context"][0][1].model_copy(update={"description": "Our own wording."})
    save(control, [mine])

    control.ensure_defaults()

    assert saved_keys(control)["context"] == mine


def test_a_saved_schema_stops_writing_kind_and_context_on_new_notes(tmp_path):
    """An install whose schema still carries the shipped defaults writes neither any more."""
    control = ControlState(tmp_path)
    control.ensure_defaults()
    written_kind = UPGRADES["kind"][0][0]
    assert written_kind.default == "note"
    save(control, [written_kind, *(form for form in UPGRADES["context"][0][1:])][:2])
    assert control.schema().defaults()["kind"] == "note"
    assert control.schema().defaults()["context"] == "internal"

    control.ensure_defaults()

    defaults = control.schema().defaults()
    assert "kind" not in defaults and "context" not in defaults
    keys = saved_keys(control)
    # Both stay declared, so a note that carries them still reads and still validates.
    assert keys["kind"].vocabulary == ["note", "artifact"]
    assert keys["context"].vocabulary == ["internal", "external", "personal"]


@pytest.mark.parametrize("form", UPGRADES["context"][0])
def test_every_earlier_context_key_is_upgraded(tmp_path, form):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    save(control, [form])
    control.ensure_defaults()
    assert saved_keys(control)["context"] == default_schema().keys[4]
    assert "context" not in control.schema().defaults()


def test_a_context_default_the_operator_chose_is_kept(tmp_path):
    control = ControlState(tmp_path)
    control.ensure_defaults()
    ours = default_schema().keys[4].model_copy(update={"default": "external"})
    revision = save(control, [ours])

    control.ensure_defaults()

    assert saved_keys(control)["context"].default == "external"
    assert control.store.read("schema").revision == revision
