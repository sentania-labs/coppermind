"""How a note becomes a file: frontmatter order, the filename and the body."""

from datetime import date, datetime, time
from pathlib import Path

from coppermind_store.notes import (
    _adoption_changes,
    _body_with_heading,
    _build_frontmatter,
    _stem_for,
    _title_of,
    _today,
)

from coppermind.schema import default_schema, note_date, note_start_time
from coppermind.settings import default_settings
from coppermind.store_protocol import CreateNote


def build(**kwargs):
    request = CreateNote(title=kwargs.pop("title", "Acme Architecture Sync"), **kwargs)
    return _build_frontmatter(request, default_schema(), default_settings(), "01ID")


def test_the_identifier_is_assigned_by_the_store_not_the_caller():
    frontmatter = build(frontmatter={"id": "a caller supplied value"})
    assert frontmatter["id"] == "01ID"


def test_missing_keys_take_their_shipped_defaults():
    frontmatter = build()
    assert frontmatter["type"] == "reference"
    assert frontmatter["context"] == "internal"
    assert frontmatter["reviewed"] is False
    assert frontmatter["sources"] == []
    assert frontmatter["schema_version"] == 1


def test_date_defaults_to_a_local_iso_datetime_without_time_or_timezone():
    frontmatter = build()
    assert "time" not in frontmatter
    assert "timezone" not in frontmatter
    assert len(frontmatter["date"]) == 19
    assert frontmatter["date"][10] == "T"
    explicit = build(frontmatter={"date": "2026-09-08"})
    assert isinstance(explicit["date"], date)


def test_keys_are_written_in_schema_order_with_a_persons_own_keys_after_them():
    frontmatter = build(frontmatter={"aliases": ["theirs"], "type": "work"})
    names = list(frontmatter)
    assert names[:3] == ["schema_version", "id", "date"]
    assert names[-1] == "aliases"


def test_null_known_keys_are_omitted_and_null_unknown_keys_are_preserved():
    frontmatter = build(frontmatter={"account": None, "aliases": ["mine"], "cssclasses": None})
    assert "account" not in frontmatter
    assert frontmatter["aliases"] == ["mine"]
    assert "cssclasses" in frontmatter
    assert frontmatter["cssclasses"] is None


def test_a_dated_type_gets_a_date_prefixed_filename():
    schema, settings = default_schema(), default_settings()
    frontmatter = build(frontmatter={"type": "work", "date": "2026-10-20T12:00:00"})
    assert _stem_for("Acme Architecture Sync", frontmatter, schema, settings) == (
        "2026-10-20 Acme Architecture Sync"
    )
    legacy = build(frontmatter={"type": "work", "date": "2026-09-08"})
    assert _stem_for("Acme Architecture Sync", legacy, schema, settings) == (
        "2026-09-08 Acme Architecture Sync"
    )


def test_an_undated_type_does_not():
    schema, settings = default_schema(), default_settings()
    frontmatter = build(frontmatter={"type": "reference", "date": "2026-09-08"})
    assert _stem_for("Runbook", frontmatter, schema, settings) == "Runbook"


def test_the_body_gets_the_title_as_its_h1():
    assert _body_with_heading("Title", "text\n") == "# Title\n\ntext\n"
    assert _body_with_heading("Title", "") == "# Title\n"


def test_internal_whitespace_in_a_title_is_collapsed_before_layout():
    request = CreateNote(title="Acme sync\n  second\tline")
    body = _body_with_heading(request.title, "")
    assert request.title == "Acme sync second line"
    assert _title_of(body, Path("unused.md")) == request.title


def test_the_requested_title_wins_over_a_heading_the_body_carries():
    """One note reports one title, whether it was just created or read back."""
    composed = _body_with_heading("Acme Sync", "# Quarterly Review\n\ntext\n")
    assert composed == "# Acme Sync\n\n# Quarterly Review\n\ntext\n"
    assert _title_of(composed, Path("Acme Sync.md")) == "Acme Sync"


def test_the_title_is_read_back_from_the_h1(tmp_path):
    assert _title_of("# Acme Architecture Sync\n\nbody", tmp_path / "x.md") == (
        "Acme Architecture Sync"
    )
    assert _title_of("no heading here", tmp_path / "Fallback.md") == "Fallback"


def test_ingest_keeps_a_supplied_legacy_time_instead_of_stamping_the_capture_moment():
    request = CreateNote(title="Plaud", frontmatter={"date": "2026-10-13", "time": "09:30"})
    frontmatter = _build_frontmatter(
        request, default_schema(), default_settings(), "01ID", from_ingest=True
    )
    assert frontmatter["date"] == date(2026, 10, 13)
    assert frontmatter["time"] == "09:30"
    assert note_start_time(frontmatter, default_schema()) == time(9, 30)


def test_adoption_does_not_stamp_today_over_a_legacy_time_that_carries_the_day():
    schema, settings = default_schema(), default_settings()
    dated = {"time": datetime(2026, 10, 14, 10, 0, 0)}
    assert "date" not in _adoption_changes(dated, schema, settings, "01ID")
    assert note_date(dated, schema) == date(2026, 10, 14)


def test_adoption_gives_a_bare_legacy_time_today_as_its_day_and_keeps_the_time():
    schema, settings = default_schema(), default_settings()
    changes = _adoption_changes({"time": "09:30"}, schema, settings, "01ID")
    assert changes["date"] == _today(settings)
    assert "time" not in changes


def test_adoption_stamps_a_note_with_neither_date_nor_time():
    schema, settings = default_schema(), default_settings()
    changes = _adoption_changes({}, schema, settings, "01ID")
    assert len(changes["date"]) == 19
    assert "time" not in changes
