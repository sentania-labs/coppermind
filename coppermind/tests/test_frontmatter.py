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


HOUSE_NOTE = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
# a person's own comment
tags:
  - architecture
---
# Ameren Architecture Sync
"""

HOUSE_PATCHED = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
# a person's own comment
tags:
  - architecture
reviewed: true
---
# Ameren Architecture Sync
"""

FLUSH_LIST_NOTE = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
tags:
- architecture
- lab
---
# Ameren Architecture Sync
"""

FLUSH_LIST_PATCHED = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
tags:
- architecture
- lab
reviewed: true
---
# Ameren Architecture Sync
"""

NESTED_MAP_NOTE = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
review:
    by: scott
    round: 1
---
# Ameren Architecture Sync
"""

NESTED_MAP_PATCHED = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
review:
    by: scott
    round: 1
reviewed: true
---
# Ameren Architecture Sync
"""

BLANK_ITEM_NOTE = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
people:
-
- scott
tags:
- architecture
---
# Ameren Architecture Sync
"""

# The blank entry is the one line a dump cannot write back as it was found.
BLANK_ITEM_PATCHED = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
people:
- 
- scott
tags:
- architecture
reviewed: true
---
# Ameren Architecture Sync
"""

MIXED_NOTE = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
tags:
- architecture
review:
    by: scott
---
# Ameren Architecture Sync
"""

# The list keeps its style and the mapping takes the house nesting.
MIXED_PATCHED = """---
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: meeting
tags:
- architecture
review:
  by: scott
reviewed: true
---
# Ameren Architecture Sync
"""


@pytest.mark.parametrize(
    ("note", "expected", "added", "removed", "trailing_space"),
    [
        (HOUSE_NOTE, HOUSE_PATCHED, ["reviewed: true"], [], []),
        (FLUSH_LIST_NOTE, FLUSH_LIST_PATCHED, ["reviewed: true"], [], []),
        (NESTED_MAP_NOTE, NESTED_MAP_PATCHED, ["reviewed: true"], [], []),
        (BLANK_ITEM_NOTE, BLANK_ITEM_PATCHED, ["reviewed: true"], [], ["- "]),
        (
            MIXED_NOTE,
            MIXED_PATCHED,
            ["  by: scott", "reviewed: true"],
            ["    by: scott"],
            [],
        ),
    ],
    ids=[
        "house style",
        "list flush with its key",
        "mapping nested four",
        "blank list entry",
        "flush list beside a mapping nested four",
    ],
)
def test_a_patch_preserves_the_note_and_its_ordinary_formatting(
    note: str,
    expected: str,
    added: list[str],
    removed: list[str],
    trailing_space: list[str],
):
    """What a targeted change keeps and what it normalises, shape by shape.

    STATUS.md and both endpoint docstrings name this test as the record, so a
    change in what survives belongs here before it belongs in the prose. Every
    line a person wrote survives except where `removed` says otherwise, and
    only the blank list entry, which a dump cannot write back as it was found,
    ends in a space.
    """
    updated = fm.patch(note, {"reviewed": True})

    assert updated == expected
    before = [line.rstrip() for line in note.splitlines()]
    after = [line.rstrip() for line in updated.splitlines()]
    assert [line for line in after if line not in before] == added
    assert [line for line in before if line not in after] == removed
    assert [line for line in updated.splitlines() if line != line.rstrip()] == trailing_space


def test_a_list_item_a_person_left_blank_still_patches():
    """A blank entry left in a property on a phone must not block a review mark."""
    updated = fm.patch(BLANK_ITEM_NOTE, {"reviewed": True})

    frontmatter, body = fm.parse(updated)
    assert frontmatter["reviewed"] is True
    assert list(frontmatter["people"]) == [None, "scott"]
    assert body.startswith("# Ameren Architecture Sync")


def test_append_missing_preserves_an_empty_frontmatter_block_and_its_line_endings():
    note = "---\r\n---\r\n# Written on a phone\r\n"

    updated = fm.append_missing(note, {"id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2"})

    assert updated == ("---\r\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\r\n---\r\n# Written on a phone\r\n")


def test_replace_scalar_changes_only_its_value():
    note = "---\r\nid: 'old' # system identity\r\ntags: [mine]\r\n---\r\n# Copy\r\n"

    updated = fm.replace_scalar(note, "id", "old", "new")

    assert updated == ("---\r\nid: 'new' # system identity\r\ntags: [mine]\r\n---\r\n# Copy\r\n")
