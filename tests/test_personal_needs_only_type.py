"""A note needs its type and nothing more: context and account hold no note back."""

from coppermind.schema import default_schema

BASE = {
    "schema_version": 1,
    "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
    "date": "2026-11-20",
    "reviewed": True,
    "sources": [],
}


def problems(**fields):
    return default_schema()._problems({**BASE, **fields})


def test_personal_note_validates_without_context():
    assert problems(type="personal") == []


def test_other_types_validate_without_context():
    assert problems(type="journal") == []
    assert problems(type="reference") == []


def test_work_note_without_context_validates():
    assert problems(type="work") == []


def test_work_note_with_context_validates():
    assert problems(type="work", context="internal") == []
    assert problems(type="work", context="external", account=["Acme"]) == []
    assert problems(type="work", context="external") == []


def test_context_already_on_a_personal_note_still_validates():
    assert problems(type="personal", context="personal") == []
    assert problems(type="personal", context="internal") == []


def test_context_description_does_not_claim_to_decide_filing():
    key = next(k for k in default_schema().keys if k.name == "context")
    assert "where the note files" not in key.description.lower()
    assert "does not decide where a note files" in key.description
