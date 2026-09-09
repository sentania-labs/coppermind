"""How a note becomes a file: frontmatter order, the filename and the body."""

from datetime import date
from pathlib import Path

from coppermind_store.notes import _body_with_heading, _build_frontmatter, _stem_for, _title_of

from coppermind.schema import default_schema
from coppermind.settings import default_settings
from coppermind.store_protocol import CreateNote


def build(**kwargs):
    request = CreateNote(title=kwargs.pop("title", "Ameren Architecture Sync"), **kwargs)
    return _build_frontmatter(request, default_schema(), default_settings(), "01ID")


def test_the_identifier_is_assigned_by_the_store_not_the_caller():
    frontmatter = build(frontmatter={"id": "a caller supplied value"})
    assert frontmatter["id"] == "01ID"


def test_missing_keys_take_their_shipped_defaults():
    frontmatter = build()
    assert frontmatter["type"] == "note"
    assert frontmatter["context"] == "internal"
    assert frontmatter["reviewed"] is False
    assert frontmatter["sources"] == []
    assert frontmatter["schema_version"] == 1


def test_the_date_defaults_to_today_and_is_written_as_a_yaml_date():
    frontmatter = build()
    assert isinstance(frontmatter["date"], date)


def test_keys_are_written_in_schema_order_with_a_persons_own_keys_after_them():
    frontmatter = build(frontmatter={"aliases": ["theirs"], "type": "meeting"})
    names = list(frontmatter)
    assert names[:4] == ["schema_version", "id", "date", "type"]
    assert names[-1] == "aliases"


def test_null_known_keys_are_omitted_and_null_unknown_keys_are_preserved():
    frontmatter = build(
        frontmatter={"account": None, "aliases": ["mine"], "cssclasses": None}
    )
    assert "account" not in frontmatter
    assert frontmatter["aliases"] == ["mine"]
    assert "cssclasses" in frontmatter
    assert frontmatter["cssclasses"] is None


def test_a_dated_type_gets_a_date_prefixed_filename():
    schema, settings = default_schema(), default_settings()
    frontmatter = build(frontmatter={"type": "meeting", "date": "2026-09-08"})
    assert _stem_for("Ameren Architecture Sync", frontmatter, schema, settings) == (
        "2026-09-08 Ameren Architecture Sync"
    )


def test_an_undated_type_does_not():
    schema, settings = default_schema(), default_settings()
    frontmatter = build(frontmatter={"type": "reference", "date": "2026-09-08"})
    assert _stem_for("Runbook", frontmatter, schema, settings) == "Runbook"


def test_the_body_gets_the_title_as_its_h1():
    assert _body_with_heading("Title", "text\n") == "# Title\n\ntext\n"
    assert _body_with_heading("Title", "") == "# Title\n"


def test_internal_whitespace_in_a_title_is_collapsed_before_layout():
    request = CreateNote(title="Ameren sync\n  second\tline")
    body = _body_with_heading(request.title, "")
    assert request.title == "Ameren sync second line"
    assert _title_of(body, Path("unused.md")) == request.title


def test_the_requested_title_wins_over_a_heading_the_body_carries():
    """One note reports one title, whether it was just created or read back."""
    composed = _body_with_heading("Ameren Sync", "# Quarterly Review\n\ntext\n")
    assert composed == "# Ameren Sync\n\n# Quarterly Review\n\ntext\n"
    assert _title_of(composed, Path("Ameren Sync.md")) == "Ameren Sync"


def test_the_title_is_read_back_from_the_h1(tmp_path):
    assert _title_of("# Ameren Architecture Sync\n\nbody", tmp_path / "x.md") == (
        "Ameren Architecture Sync"
    )
    assert _title_of("no heading here", tmp_path / "Fallback.md") == "Fallback"
