import pytest

from coppermind import frontmatter as fm

NOTE = """---
schema_version: 1
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
date: 2026-09-08
type: meeting
# a person's own comment
their_own_key: keep me
tags:
  - architecture
---
# Ameren Architecture Sync

## Key points
- Target architecture agreed
"""


def test_parses_frontmatter_and_body():
    frontmatter, body = fm.parse(NOTE)
    assert frontmatter["id"] == "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
    assert body.startswith("# Ameren Architecture Sync")


def test_a_file_without_frontmatter_is_all_body():
    frontmatter, body = fm.parse("# Just a note\n")
    assert frontmatter == {}
    assert body == "# Just a note\n"


def test_an_unclosed_block_is_reported_rather_than_guessed_at():
    with pytest.raises(fm.FrontmatterError):
        fm.parse("---\nid: x\n# no closing delimiter\n")


def test_a_write_touches_only_the_key_it_changes():
    """The reason this matters: every system write syncs to every device.

    A YAML load and dump would reorder the keys and drop the comment, and
    Obsidian Sync would then push a rewritten file to every device for a one
    field change.
    """
    updated = fm.patch(NOTE, {"reviewed": True})
    before = NOTE.splitlines()
    after = updated.splitlines()
    assert "reviewed: true" in after
    # Every original line survives, in its original order.
    assert [line for line in after if line in before] == before
    assert "# a person's own comment" in updated
    assert "their_own_key: keep me" in updated


def test_unknown_keys_survive_a_round_trip():
    frontmatter, body = fm.parse(NOTE)
    assert frontmatter["their_own_key"] == "keep me"
    assert "their_own_key: keep me" in fm.compose(frontmatter, body)


def test_unset_removes_a_key():
    updated = fm.patch(NOTE, {}, unset=["their_own_key"])
    assert "their_own_key" not in updated


def test_composing_an_empty_mapping_yields_a_plain_body():
    assert fm.compose({}, "# Title\n") == "# Title\n"
