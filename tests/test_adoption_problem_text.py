"""Every reason adoption can refuse a file reaches Problems as a plain sentence."""

from __future__ import annotations

import re

import pytest
from coppermind_store import notes
from coppermind_store.reconciler import adoption_problem

CAUSES = [
    *notes.FIXED_ADOPTION_CAUSES,
    notes.CLAUSE_MISSING + "account",
    notes.CLAUSE_MISSING + "account, context",
    notes.CLAUSE_REFUSED_CHOICE + "type",
    notes.CLAUSE_REFUSED_VALUE + "tags",
    notes.CLAUSE_DEFAULT + "type",
    notes.CLAUSE_MISSING + "account; " + notes.CLAUSE_REFUSED_CHOICE + "type",
    # What the frontmatter parser reports, by category.
    "unterminated_block",
    "not_a_mapping",
    "unsupported_line_endings",
    "scanner",
    "parser",
    "constructor",
    "duplicate_key",
    # A cause this code has never seen.
    "some_future_cause",
    "Something Unforeseen Happened",
]
INTERNAL_WORDS = ("schema", "frontmatter", "mirror", "UTF-8 ", "Fields and tags")


@pytest.mark.parametrize("cause", CAUSES)
def test_a_cause_renders_as_a_plain_sentence_with_a_remedy(cause: str):
    action, text = adoption_problem(cause)

    assert action
    assert text.startswith("Coppermind left this note unchanged") or text.startswith(
        "Coppermind could not identify this note"
    )
    assert "Coppermind checks again on its own" in text or "next check" in text
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", text), text
    assert cause not in text or " " in cause and cause.startswith("Coppermind")
    for word in INTERNAL_WORDS:
        assert word not in text, text


def test_a_missing_value_is_not_called_an_invalid_choice():
    _, text = adoption_problem(notes.CLAUSE_MISSING + "account")

    assert "This note needs an account" in text
    assert "allowed choice" not in text


def test_a_missing_mixed_case_key_keeps_its_spelling():
    _, text = adoption_problem(notes.CLAUSE_MISSING + "dueDate")

    assert "This note needs a dueDate." in text
    assert "duedate" not in text


def test_line_endings_say_what_to_change():
    _, text = adoption_problem("unsupported_line_endings")

    assert "line endings aren't supported" in text and "LF or CRLF" in text
    assert "unsupported_line_endings" not in text
