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
