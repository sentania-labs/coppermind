"""Alias normalization and closed tags, without a database.

Every store write path reaches one of four helpers: `_build_frontmatter`
(create and ingest), `_replacement_frontmatter` (whole document replace),
`_with_kinds` (frontmatter patch) and `_with_canonical_tags` (reconciler
adoption). These pin what each does with a tag list; the PostgreSQL backed
write paths are proven in `tests/integration/test_tags.py`.
"""

from __future__ import annotations

import pytest
from coppermind_store.notes import (
    _build_frontmatter,
    _replacement_frontmatter,
    _with_canonical_tags,
    _with_kinds,
)
from pydantic import ValidationError

from coppermind import frontmatter as fm
from coppermind.schema import FrontmatterSchema, default_schema
from coppermind.settings import default_settings
from coppermind.store_protocol import CreateNote, ReplaceNote

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"


def schema_with(
    tags: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
    mode: str = "open",
) -> FrontmatterSchema:
    body = default_schema().model_dump(mode="json")
    body["tags"] = tags or {}
    body["tag_aliases"] = aliases or {}
    body["tag_mode"] = mode
    return FrontmatterSchema.model_validate(body)


ALIASED = schema_with(
    tags={"work": "The day job.", "home": ""},
    aliases={"job": "work", "office": "work", "house": "home"},
)


def test_an_alias_becomes_its_canonical_tag():
    assert ALIASED.normalize_tags(["job", "house", "fishing"]) == ["work", "home", "fishing"]


def test_repeats_collapse_and_the_person_s_order_is_kept():
    assert ALIASED.normalize_tags(["home", "job", "work", "office", "home"]) == ["home", "work"]
    assert ALIASED.normalize_tags(["zebra", "apple"]) == ["zebra", "apple"]


def test_a_clean_list_comes_back_equal_so_nothing_is_written():
    tags = ["fishing", "work", "home"]
    assert ALIASED.normalize_tags(tags) == tags


def test_an_alias_matches_exactly():
    assert ALIASED.normalize_tags(["Job", "JOB"]) == ["Job", "JOB"]


def test_a_value_that_is_not_text_is_left_alone():
    assert ALIASED.normalize_tags([2026, "job", True]) == [2026, "work", True]


def test_the_shipped_schema_has_no_aliases_and_open_tags():
    schema = default_schema()
    assert schema.tag_aliases == {}
    assert schema.tag_mode == "open"
    assert schema.normalize_tags(["a", "b"]) == ["a", "b"]


def test_create_and_ingest_normalize_tags():
    """Ingest builds its note through the same `_build_frontmatter` create uses."""
    request = CreateNote(title="Standup", frontmatter={"tags": ["job", "home", "office"]})
    built = _build_frontmatter(request, ALIASED, default_settings(), NOTE_ID)
    assert built["tags"] == ["work", "home"]


def test_replace_normalizes_tags():
    request = ReplaceNote(frontmatter={"tags": ["house", "job"]}, body="")
    assert _replacement_frontmatter(request, ALIASED, NOTE_ID)["tags"] == ["home", "work"]


def test_patch_normalizes_tags_and_leaves_other_keys_as_sent():
    changes = _with_kinds({"tags": ["office"], "reviewed": True}, ALIASED)
    assert changes == {"tags": ["work"], "reviewed": True}


def test_a_tags_value_that_is_not_a_list_is_left_for_validation():
    assert _with_kinds({"tags": "job"}, ALIASED) == {"tags": "job"}


def test_adoption_rewrites_only_the_tag_lines():
    head = "---\r\nmy_key: 'hand written' # keep this comment\r\n"
    after = "# a comment after the tags\r\nreviewed: false\r\n---\r\n# Phone tags\r\n"
    text = f"{head}tags:\r\n  - job\r\n  - home\r\n  - job\r\n{after}"
    assert _with_canonical_tags(text, ALIASED) == f"{head}tags:\r\n  - work\r\n  - home\r\n{after}"


def test_adoption_keeps_a_flow_list_in_flow_style():
    text = "---\ntags: [house, home] \nid: x\n---\n"
    assert _with_canonical_tags(text, ALIASED) == "---\ntags: [home]\nid: x\n---\n"


def test_adoption_leaves_clean_or_commented_tags_byte_exact():
    clean = "---\r\ntags: [work, home]\r\n---\r\nbody\r\n"
    commented = "---\ntags:\n  - job # from the old system\n---\n"
    assert _with_canonical_tags(clean, ALIASED) == clean
    assert _with_canonical_tags(commented, ALIASED) == commented
    assert _with_canonical_tags("# no frontmatter\n", ALIASED) == "# no frontmatter\n"


def test_adopted_text_reads_back_with_canonical_tags():
    text = "---\ntags:\n- office\n- fishing\n---\n# Flush list\n"
    adopted = _with_canonical_tags(text, ALIASED)
    assert adopted == "---\ntags:\n- work\n- fishing\n---\n# Flush list\n"
    assert fm.parse(adopted)[0]["tags"] == ["work", "fishing"]


def test_open_tags_accept_anything():
    assert ALIASED.tag_problems({"tags": ["fishing"]}) == []


def test_closed_tags_refuse_an_unlisted_tag_and_accept_an_alias():
    closed = schema_with(tags={"work": ""}, aliases={"job": "work"}, mode="closed")
    assert closed.tag_problems({"tags": ["work", "job"]}) == []
    assert closed.tag_problems({"tags": ["work", "fishing"]}) == [
        "tags: 'fishing' is not a listed tag, and tags are closed"
    ]


def test_closed_tags_never_refuse_a_tag_the_note_already_carries():
    closed = schema_with(tags={"work": ""}, aliases={"job": "work"}, mode="closed")
    current = {"tags": ["fishing"]}
    assert closed.tag_problems({"tags": ["fishing", "job"]}, current) == []
    assert closed.tag_problems({"tags": ["fishing", "golf"]}, current) == [
        "tags: 'golf' is not a listed tag, and tags are closed"
    ]


def test_closed_tags_do_not_change_what_validation_reports():
    """Existing validation behaves the same for every existing key."""
    closed = schema_with(tags={"work": ""}, mode="closed")
    frontmatter = {
        "schema_version": 1,
        "id": NOTE_ID,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": ["fishing"],
    }
    assert closed.validate_frontmatter(frontmatter) == []
    assert closed.invalid_keys(frontmatter) == []
    assert default_schema().validate_frontmatter(frontmatter) == []


@pytest.mark.parametrize(
    "tags,aliases,mode,message",
    [
        ({}, {"job": "job"}, "open", "names itself"),
        ({"job": ""}, {"job": "work"}, "open", "also a listed tag"),
        ({}, {"job": "work", "work": "labour"}, "open", "itself an alias"),
        ({"work": ""}, {"chores": "home"}, "closed", "not listed"),
        ({"two words": ""}, {}, "open", "spaces"),
        ({}, {"#job": "work"}, "open", "start with #"),
        ({}, {"job": "a,b"}, "open", "commas"),
        ({"": ""}, {}, "open", "empty"),
    ],
)
def test_inconsistent_tag_rules_are_refused(tags, aliases, mode, message):
    with pytest.raises(ValidationError, match=message):
        schema_with(tags=tags, aliases=aliases, mode=mode)


def test_aliases_of_lists_every_alias_of_a_tag():
    assert ALIASED.aliases_of("work") == ["job", "office"]
    assert ALIASED.aliases_of("fishing") == []
