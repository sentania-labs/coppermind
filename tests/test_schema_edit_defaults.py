"""An Admin edit may not save a default the field's own allowed values refuse."""

from __future__ import annotations

from coppermind.schema import FrontmatterSchema, default_schema


def _with(**changes: object) -> FrontmatterSchema:
    schema = default_schema()
    keys = [key.model_copy(update=changes) if key.name == "type" else key for key in schema.keys]
    return schema.model_copy(update={"keys": keys})


def test_the_shipped_schema_has_no_edit_problems():
    assert default_schema().edit_problems() == []


def test_an_enum_default_outside_its_allowed_values_is_refused_plainly():
    problems = _with(default="note").edit_problems()

    assert len(problems) == 1
    assert problems[0].startswith("type: the default 'note' is not one of the allowed values")


def test_removing_the_allowed_value_the_default_uses_is_refused():
    schema = default_schema()
    type_key = next(key for key in schema.keys if key.name == "type")
    assert type_key.default in type_key.vocabulary
    trimmed = [v for v in type_key.vocabulary if v != type_key.default]

    assert any("default" in p for p in _with(vocabulary=trimmed).edit_problems())


def test_required_when_values_must_be_allowed_values_of_the_watched_key():
    schema = default_schema()
    keys = [
        key.model_copy(update={"required_when": "type", "required_when_values": ["nonsense"]})
        if key.name == "account"
        else key
        for key in schema.keys
    ]
    problems = schema.model_copy(update={"keys": keys}).edit_problems()

    assert any("nonsense" in p and "does not allow" in p for p in problems)


def test_a_schema_file_with_a_bad_default_still_loads():
    document = _with(default="note").model_dump(mode="json")

    loaded = FrontmatterSchema.model_validate(document)

    assert next(key for key in loaded.keys if key.name == "type").default == "note"
    assert loaded.edit_problems()
