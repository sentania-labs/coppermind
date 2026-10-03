"""Tags as data: alias normalization, closed tags and counts, against PostgreSQL.

The unit tests in `services/store/tests/test_tag_aliases.py` pin the rules;
these prove each store write path applies them and that the counts Admin shows
come from the mirror as notes, not occurrences.
"""

from __future__ import annotations

import pytest
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once

from coppermind import frontmatter as fm
from coppermind.store_protocol import (
    CreateNote,
    PatchFrontmatter,
    ReplaceNote,
    TagCount,
    ValidationFailed,
)
from tests.integration.test_ingest_write_protocol import sample


def set_tag_rules(
    store: LocalStore,
    *,
    tags: dict[str, str],
    aliases: dict[str, str],
    mode: str = "open",
) -> None:
    """Write the schema file the way the Admin Fields and tags page does."""
    state = store.control.store.read("schema")
    body = dict(state.body)
    body["tags"] = tags
    body["tag_aliases"] = aliases
    body["tag_mode"] = mode
    store.control.store.write("schema", body, if_revision=state.revision)


async def test_tag_counts_are_notes_not_occurrences(store: LocalStore):
    """A note whose file repeats a tag counts once for it."""
    first = await store.create_note(CreateNote(title="First", frontmatter={"tags": ["work"]}))
    await store.create_note(CreateNote(title="Second", frontmatter={"tags": ["work", "home"]}))
    path = store.notes_root / first.path
    # A device edit repeats the tag. Reconciling a known note mirrors what the
    # file holds rather than rewriting it, so the row carries the repeat.
    path.write_bytes(path.read_bytes().replace(b"  - work\n", b"  - work\n  - work\n"))
    await reconcile_once(store)
    assert (await store.get_note(first.id)).frontmatter["tags"] == ["work", "work"]

    assert await store.list_tags() == [
        TagCount(tag="home", count=1),
        TagCount(tag="work", count=2),
    ]


async def test_a_missing_note_is_not_counted(store: LocalStore):
    await store.create_note(CreateNote(title="Kept", frontmatter={"tags": ["work"]}))
    gone = await store.create_note(CreateNote(title="Gone", frontmatter={"tags": ["work"]}))
    (store.notes_root / gone.path).unlink()
    await reconcile_once(store)

    assert await store.list_tags() == [TagCount(tag="work", count=1)]


async def test_create_replaces_an_alias_with_its_tag(store: LocalStore):
    set_tag_rules(store, tags={"work": "The day job."}, aliases={"job": "work"})
    note = await store.create_note(
        CreateNote(title="Standup", frontmatter={"tags": ["job", "home", "work"]})
    )

    assert note.frontmatter["tags"] == ["work", "home"]
    frontmatter, _ = fm.parse((store.notes_root / note.path).read_text(encoding="utf-8"))
    assert frontmatter["tags"] == ["work", "home"]
    assert await store.list_tags() == [
        TagCount(tag="home", count=1),
        TagCount(tag="work", count=1),
    ]


async def test_ingest_replaces_an_alias_with_its_tag(store: LocalStore):
    set_tag_rules(store, tags={}, aliases={"arch": "architecture"})
    request = sample()
    request.note.frontmatter["tags"] = ["arch", "vcf"]

    ingested = await store.ingest(request)
    note = await store.get_note(ingested.note.id)

    assert note.frontmatter["tags"] == ["architecture", "vcf"]


async def test_patch_and_replace_replace_an_alias_with_its_tag(store: LocalStore):
    note = await store.create_note(CreateNote(title="Plan", frontmatter={"tags": ["job"]}))
    set_tag_rules(store, tags={}, aliases={"job": "work"})

    patched = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"tags": ["job", "plans"]}), note.content_hash
    )
    assert patched.frontmatter["tags"] == ["work", "plans"]

    sent = {**patched.frontmatter, "tags": ["plans", "job"]}
    replaced = await store.replace_note(
        note.id, ReplaceNote(frontmatter=sent, body=patched.body), patched.content_hash
    )
    assert replaced.frontmatter["tags"] == ["plans", "work"]


async def test_closed_tags_refuse_an_unlisted_tag_on_create_and_ingest(store: LocalStore):
    set_tag_rules(store, tags={"work": ""}, aliases={"job": "work"}, mode="closed")

    with pytest.raises(ValidationFailed) as refused:
        await store.create_note(
            CreateNote(title="Hobby", frontmatter={"tags": ["work", "fishing"]})
        )
    assert any("'fishing'" in problem for problem in refused.value.errors)
    assert not list(store.notes_root.rglob("*.md"))

    request = sample()
    request.note.frontmatter["tags"] = ["fishing"]
    with pytest.raises(ValidationFailed):
        await store.ingest(request)

    # A listed tag, and an alias of one, are accepted.
    note = await store.create_note(CreateNote(title="Day", frontmatter={"tags": ["job"]}))
    assert note.frontmatter["tags"] == ["work"]


async def test_closed_tags_refuse_only_what_a_write_adds(store: LocalStore):
    """A note tagged before tags were closed can still be reviewed and edited."""
    note = await store.create_note(CreateNote(title="Older", frontmatter={"tags": ["fishing"]}))
    set_tag_rules(store, tags={"work": ""}, aliases={}, mode="closed")

    reviewed = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    assert reviewed.frontmatter["tags"] == ["fishing"]

    kept = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"tags": ["fishing", "work"]}), reviewed.content_hash
    )
    assert kept.frontmatter["tags"] == ["fishing", "work"]

    with pytest.raises(ValidationFailed) as refused:
        await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"tags": ["fishing", "golf"]}), kept.content_hash
        )
    assert any("'golf'" in problem for problem in refused.value.errors)

    with pytest.raises(ValidationFailed):
        await store.replace_note(
            note.id,
            ReplaceNote(frontmatter={**kept.frontmatter, "tags": ["golf"]}, body=kept.body),
            kept.content_hash,
        )
    current = await store.get_note(note.id)
    assert current.content_hash == kept.content_hash


async def test_adoption_rewrites_only_the_tag_lines(store: LocalStore):
    """Adoption stays append only for every byte that is not the tag list."""
    set_tag_rules(store, tags={"work": ""}, aliases={"job": "work"})
    unknown = store.notes_root / "Review" / "Phone tags.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    head = "---\r\nmy_key: 'hand written' # keep this comment\r\n"
    after = "# a comment after the tags\r\nreviewed: false\r\n"
    end = "---\r\n# Phone tags\r\n"
    unknown.write_bytes(f"{head}tags:\r\n  - job\r\n  - home\r\n  - job\r\n{after}{end}".encode())

    counts = await reconcile_once(store)
    adopted = unknown.read_bytes().decode("utf-8")

    assert counts["adopted"] == 1
    # The tag lines changed; what adoption appends lands before the closing
    # delimiter, as it always has, and nothing else moved.
    assert adopted.startswith(f"{head}tags:\r\n  - work\r\n  - home\r\n{after}")
    assert adopted.endswith(end)
    assert "\n" not in adopted.replace("\r\n", "")
    frontmatter, _ = fm.parse(adopted)
    assert frontmatter["tags"] == ["work", "home"]
    assert (await store.get_note(frontmatter["id"])).frontmatter["tags"] == ["work", "home"]


async def test_adoption_leaves_a_commented_tag_list_as_written(store: LocalStore):
    set_tag_rules(store, tags={}, aliases={"job": "work"})
    unknown = store.notes_root / "Review" / "Commented tags.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    original = "---\ntags:\n  - job # from the old system\n---\n# Commented tags\n"
    unknown.write_bytes(original.encode())

    counts = await reconcile_once(store)
    adopted = unknown.read_text(encoding="utf-8")

    assert counts["adopted"] == 1
    assert adopted.startswith("---\ntags:\n  - job # from the old system\n")


async def test_closed_tags_never_refuse_adoption(store: LocalStore):
    """Tags are standardized after the fact; a device note still gets its identity."""
    set_tag_rules(store, tags={"work": ""}, aliases={}, mode="closed")
    unknown = store.notes_root / "Review" / "Unlisted.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    unknown.write_bytes(b"---\ntags: [fishing]\n---\n# Unlisted\n")

    counts = await reconcile_once(store)
    frontmatter, _ = fm.parse(unknown.read_text(encoding="utf-8"))

    assert counts["adopted"] == 1
    assert frontmatter["tags"] == ["fishing"]
    assert await store.list_tags() == [TagCount(tag="fishing", count=1)]
