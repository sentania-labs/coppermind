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
