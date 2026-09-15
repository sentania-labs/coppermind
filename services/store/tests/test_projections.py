"""What a generated projection is allowed to replace.

Projections live in the notes filesystem, where Obsidian Sync writes the same
folders this store does, so the only file `write_projection` may rename over is
generated output that still names the source it is regenerating.
"""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from coppermind_store import projections as projections_module
from coppermind_store.projections import new_projection_path, write_projection

from coppermind import frontmatter as fm
from coppermind.settings import default_settings
from coppermind.store_protocol import ProjectionNotPlaced

SOURCE_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
OTHER_SOURCE_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W9"
MINE = "---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n# The captain's own note\n"
ANOTHER_SOURCE = fm.compose(
    {"schema_version": 1, "managed": True, "source_id": OTHER_SOURCE_ID, "source_revision": 1},
    "# Another source (source)\n",
)


def place(notes_root: Path) -> str:
    return new_projection_path(
        notes_root,
        default_settings(),
        provider="plaud",
        title="Ameren Architecture Sync",
        note_date=date(2026, 9, 8),
    )


def write(
    notes_root: Path,
    relative_path: str,
    *,
    revision: int = 1,
    text: str = "first",
) -> bool:
    artifact: dict[str, Any] = {
        "name": "transcript.txt",
        "mime_type": "text/plain",
        "size_bytes": len(text.encode("utf-8")),
        "sha256": "abc",
    }
    return write_projection(
        notes_root,
        default_settings(),
        source_id=SOURCE_ID,
        revision=revision,
        title="Ameren Architecture Sync",
        revision_ingested_at=datetime(2026, 9, 8, 19, 2, 11, tzinfo=UTC),
        artifacts=[(artifact, text.encode("utf-8"))],
        relative_path=relative_path,
    )


def test_a_later_revision_replaces_the_projection_it_finds(tmp_path: Path):
    relative = place(tmp_path)
    assert write(tmp_path, relative) is True

    assert write(tmp_path, relative, revision=2, text="second") is False

    frontmatter, body = fm.parse((tmp_path / relative).read_text(encoding="utf-8"))
    assert frontmatter["source_revision"] == 2
    assert "second" in body


@pytest.mark.parametrize(
    "occupant", [MINE, ANOTHER_SOURCE], ids=["his own note", "another source projection"]
)
def test_a_file_this_source_did_not_generate_is_never_written_over(tmp_path: Path, occupant: str):
    relative = place(tmp_path)
    write(tmp_path, relative)
    target = tmp_path / relative
    target.write_text(occupant, encoding="utf-8")

    with pytest.raises(ProjectionNotPlaced) as refused:
        write(tmp_path, relative, revision=2, text="second")

    assert target.read_text(encoding="utf-8") == occupant
    assert refused.value.source_id == SOURCE_ID
    assert refused.value.revision == 2
    assert refused.value.path == relative


def test_a_file_delivered_while_the_bytes_are_staged_is_not_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Staging is the slow half, so the check has to come after it.

    A device can deliver the captain's own note onto this path at any moment.
    Composing and fsyncing a whole transcript in front of the check would leave
    that window open for as long as the document is large.
    """
    relative = place(tmp_path)
    write(tmp_path, relative)
    target = tmp_path / relative
    real_stage = projections_module.stage_bytes

    def stage_then_sync_delivers(path: Path, data: bytes, **kwargs: Any) -> Path:
        staged = real_stage(path, data, **kwargs)
        path.write_text(MINE, encoding="utf-8")
        return staged

    monkeypatch.setattr(projections_module, "stage_bytes", stage_then_sync_delivers)

    with pytest.raises(ProjectionNotPlaced):
        write(tmp_path, relative, revision=2, text="second")

    assert target.read_text(encoding="utf-8") == MINE
    assert not list(target.parent.glob(".*.tmp"))


def test_a_symlink_at_the_chosen_name_is_refused_rather_than_followed(tmp_path: Path):
    """A link is not generated output, and its target is not this folder.

    A projection landing wherever a link points leaves the store-owned folder,
    which is the one thing keeping transcripts out of adoption and out of the
    notes Git history.
    """
    relative = place(tmp_path)
    link = tmp_path / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    escaped = tmp_path / "Review" / "escaped.md"
    link.symlink_to(escaped)

    with pytest.raises(ProjectionNotPlaced) as refused:
        write(tmp_path, relative)

    assert not escaped.exists()
    assert link.is_symlink()
    assert refused.value.path == relative


def test_a_directory_at_the_chosen_name_is_refused_rather_than_called_an_outage(tmp_path: Path):
    """Something that is not a readable file is an occupied name, not a bad mount.

    Answering that the volume is unwell sends the operator to a healthy mount
    to look for a fault that is not there. The name is taken, which is what
    the refusal has to say, and a page chosen afresh goes to a free one.
    """
    relative = place(tmp_path)
    standing = tmp_path / relative
    standing.mkdir(parents=True)

    with pytest.raises(ProjectionNotPlaced) as refused:
        write(tmp_path, relative)

    assert standing.is_dir()
    assert not list(standing.iterdir())
    assert not list(standing.parent.glob(".*.tmp"))
    assert refused.value.path == relative


def test_a_notes_root_reached_through_a_link_still_names_a_page_under_it(tmp_path: Path):
    """The chosen name is composed from the settings, not read back off the disk.

    An operator may mount or link `/data/notes` through a symlinked parent.
    Deriving the name from the resolved path would answer that the volume is
    unwell on every ingest, while the volume is healthy.
    """
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "notes"
    linked.symlink_to(real)

    relative = place(linked)

    assert relative == "_Sources/Plaud/2026-09-08 Ameren Architecture Sync.md"
    assert write(linked, relative) is True
    assert (real / relative).is_file()


def test_a_directory_standing_at_the_preferred_name_is_a_taken_name(tmp_path: Path):
    """Choosing a name has to see every entry, not only the regular files.

    A directory at the name a page would take is as much in the way as a file
    at it. Counting only files hands back the occupied name every time, so no
    retry ever places the page.
    """
    occupied = tmp_path / "_Sources" / "Plaud" / "2026-09-08 Ameren Architecture Sync.md"
    occupied.mkdir(parents=True)

    relative = place(tmp_path)

    assert relative == "_Sources/Plaud/2026-09-08 Ameren Architecture Sync (2).md"
    assert write(tmp_path, relative) is True
    assert (tmp_path / relative).is_file()
    assert occupied.is_dir() and not list(occupied.iterdir())
