import pytest
from pydantic import ValidationError

from coppermind.schema import FrontmatterSchema, default_schema


def test_a_schema_missing_a_required_role_is_rejected():
    body = default_schema().model_dump(mode="json")
    del body["roles"]["sources_key"]
    with pytest.raises(ValidationError, match="sources_key"):
        FrontmatterSchema.model_validate(body)


def test_a_schema_role_pointing_at_an_undefined_key_is_rejected():
    body = default_schema().model_dump(mode="json")
    body["roles"]["sources_key"] = "source_ids"
    with pytest.raises(ValidationError, match="sources_key=source_ids"):
        FrontmatterSchema.model_validate(body)


def test_a_fresh_install_has_working_defaults():
    defaults = default_schema().defaults()
    assert defaults["type"] == "reference"
    assert defaults["context"] == "internal"
    assert defaults["reviewed"] is False
    assert defaults["sources"] == []


@pytest.mark.parametrize("note_type", ["work", "personal", "reference", "journal"])
@pytest.mark.parametrize("context", ["internal", "external", "personal"])
def test_a_complete_note_validates(note_type, context):
    schema = default_schema()
    assert (
        schema.validate_frontmatter(
            {
                "schema_version": 1,
                "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
                "date": "2026-09-08",
                "type": note_type,
                "context": context,
                "account": "Acme",
                "reviewed": False,
                "sources": [],
            }
        )
        == []
    )


def test_a_value_outside_the_vocabulary_is_reported():
    schema = default_schema()
    problems = schema.validate_frontmatter(
        {
            "schema_version": 1,
            "id": "x",
            "date": "2026-09-08",
            "type": "podcast",
            "context": "internal",
            "reviewed": False,
            "sources": [],
        }
    )
    assert any("podcast" in problem for problem in problems)


def test_default_schema_requires_account_for_external_notes():
    schema = default_schema()
    base = {
        "schema_version": 1,
        "id": "x",
        "date": "2026-09-08",
        "type": "work",
        "context": "external",
        "reviewed": False,
        "sources": [],
    }
    assert any(problem.startswith("account:") for problem in schema.validate_frontmatter(base))
    assert schema.validate_frontmatter({**base, "account": "Acme"}) == []


def test_a_missing_required_key_is_reported():
    problems = default_schema().validate_frontmatter({"type": "reference"})
    assert any(problem.startswith("id:") for problem in problems)


def test_a_persons_own_keys_are_not_problems():
    schema = default_schema()
    problems = schema.validate_frontmatter(
        {
            "schema_version": 1,
            "id": "x",
            "date": "2026-09-08",
            "type": "reference",
            "context": "internal",
            "reviewed": False,
            "sources": [],
            "aliases": ["something they added themselves"],
        }
    )
    assert problems == []


@pytest.mark.parametrize("context", ["internal", "personal"])
def test_account_is_optional_outside_external_context(context):
    schema = default_schema()
    frontmatter = {
        **schema.defaults(),
        "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
        "date": "2026-09-08",
        "context": context,
    }
    assert "account" not in frontmatter
    assert schema.validate_frontmatter(frontmatter) == []
