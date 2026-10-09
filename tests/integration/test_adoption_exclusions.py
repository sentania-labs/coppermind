"""Tests for reconciler adoption exclusion rules.

Covers:
- Default excluded folders (Templates, _Templates)
- Custom excluded folders
- Default excluded patterns (*.excalidraw.md, *.canvas, *.kanban.md)
- Custom excluded patterns
- Frontmatter plugin markers (excalidraw: true, kanban-plugin: true)
- Already-identified files in excluded places are still followed
- Exclusion count in status
- Exclusion problems in the problems list
- Settings changes take effect on next pass
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note
from coppermind.ids import new_id

_DEFAULT_EXCLUDED_FOLDERS = ["Templates", "_Templates"]
_DEFAULT_EXCLUDED_PATTERNS = ["*.excalidraw.md", "*.canvas", "*.kanban.md"]


def _write_note_file(root, relative, id_value, frontmatter_extra=None, body=""):
    """Write a Markdown file at `relative` under `root` with given id and extra frontmatter."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = f"---\nid: {id_value}\n"
    if frontmatter_extra:
        for k, v in frontmatter_extra.items():
            if isinstance(v, list):
                fm_lines += f"{k}:\n"
                for item in v:
                    fm_lines += f"- {item}\n"
            else:
                fm_lines += f"{k}: {v}\n"
    fm_lines += "---\n"
    content = f"{fm_lines}{body}\n"
    path.write_text(content, encoding="utf-8")
    return path


def _apply_settings(store, excluded_folders=None, excluded_patterns=None):
    """Update the settings file with new exclusion values."""
    current = store.control.store.read("settings")
    body = dict(current.body)
    body["reconcile"] = {
        **body.get("reconcile", {}),
        "excluded_folders": excluded_folders
            if excluded_folders is not None
            else list(_DEFAULT_EXCLUDED_FOLDERS),
        "excluded_patterns": excluded_patterns
            if excluded_patterns is not None
            else list(_DEFAULT_EXCLUDED_PATTERNS),
    }
    body.pop("revision", None)
    store.control.store.write("settings", body, if_revision=current.revision)


class TestIntegrationExclusions:
    """Integration tests for the reconciler with exclusion rules."""

    @pytest.mark.asyncio
    async def test_default_excluded_folders_are_not_adopted(self, store):
        """Files in Templates/ and _Templates/ are excluded by default."""
        root = store.notes_root
        id1 = new_id()
        id2 = new_id()
        id3 = new_id()
        # Create files in both default excluded folders.
        _write_note_file(root, "Templates/my template.md", id1, {"type": "template"})
        _write_note_file(root, "_Templates/another.md", id2, {"type": "template"})
        # Also create one in the review folder that should be adopted.
        _write_note_file(
            root, "Review/regular note.md", id3, {"type": "note"}
        )

        counts = await reconcile_once(store, full=True)  # noqa: F841

        # The exclusion count should be at least 2.
        count = store.control.read_reconciler_excluded_count()
        assert count >= 2

        # The regular note should be adopted (mirrored row exists).
        async with store.session_factory() as session:
            row = (
                await session.scalars(sa.select(Note).where(Note.id == id3))
            ).one_or_none()
            assert row is not None, "note should have been adopted"

    @pytest.mark.asyncio
    async def test_custom_excluded_folder(self, store):
        """A custom excluded folder not in defaults is also excluded."""
        _apply_settings(store, excluded_folders=["Templates", "Archive"])

        root = store.notes_root
        id1 = new_id()
        id2 = new_id()
        id3 = new_id()
        _write_note_file(root, "Archive/file.md", id1, {"type": "note"})
        _write_note_file(root, "Templates/skip.md", id2, {"type": "note"})
        _write_note_file(root, "Review/keep.md", id3, {"type": "note"})

        await reconcile_once(store, full=True)

        count = store.control.read_reconciler_excluded_count()
        assert count >= 2

        # Both should be in exclusions
        exclusions = store.control.read_reconciler_exclusions()
        assert "Archive/file.md" in exclusions
        assert "Templates/skip.md" in exclusions

    @pytest.mark.asyncio
    async def test_custom_excluded_pattern(self, store):
        """Custom glob patterns exclude matching files."""
        _apply_settings(
            store,
            excluded_patterns=[
                "*.excalidraw.md",
                "*.canvas",
                "*.kanban.md",
                "*.tmp.md",
            ],
        )

        root = store.notes_root
        id1 = new_id()
        id2 = new_id()
        id3 = new_id()
        _write_note_file(root, "Review/drawing.excalidraw.md", id1, {"type": "note"})
        _write_note_file(root, "Review/temp.tmp.md", id2, {"type": "note"})
        _write_note_file(root, "Review/regular.md", id3, {"type": "note"})

        await reconcile_once(store, full=True)

        count = store.control.read_reconciler_excluded_count()
        assert count >= 2

        exclusions = store.control.read_reconciler_exclusions()
        assert "Review/drawing.excalidraw.md" in exclusions
        assert "Review/temp.tmp.md" in exclusions

    @pytest.mark.asyncio
    async def test_frontmatter_marker_excludes(self, store):
        """Files with excalidraw or kanban-plugin markers are excluded."""
        # Disable default patterns so file name doesn't trigger exclusion.
        _apply_settings(
            store,
            excluded_patterns=["*.tmp.md"],
        )

        root = store.notes_root
        id1 = new_id()
        id2 = new_id()
        id3 = new_id()
        _write_note_file(
            root,
            "Review/drawing.md",
            id1,
            {"excalidraw": "true", "type": "drawing"},
        )
        _write_note_file(
            root,
            "Review/kanban.md",
            id2,
            {"kanban-plugin": "framework", "type": "board"},
        )
        _write_note_file(
            root, "Review/normal.md", id3, {"type": "note"}
        )

        await reconcile_once(store, full=True)

        count = store.control.read_reconciler_excluded_count()
        assert count >= 2

        exclusions = store.control.read_reconciler_exclusions()
        assert "Review/drawing.md" in exclusions
        assert "Review/kanban.md" in exclusions

    @pytest.mark.asyncio
    async def test_already_identified_in_excluded_folder_is_followed(self, store):
        """A file with a known identity in an excluded folder is still tracked."""
        # First, create and adopt a note normally.
        note_id = new_id()
        _write_note_file(
            store.notes_root, "Review/original.md", note_id, {"type": "note"}
        )

        await reconcile_once(store, full=True)

        # Now move that note into an excluded folder.
        moved_path = store.notes_root / "Templates/original.md"
        original_data = (store.notes_root / "Review/original.md").read_bytes()
        moved_path.parent.mkdir(parents=True, exist_ok=True)
        moved_path.write_bytes(original_data)
        (store.notes_root / "Review/original.md").unlink()

        await reconcile_once(store, full=True)

        # The note should still be mirrored (not reported missing).
        async with store.session_factory() as session:
            row = (
                await session.scalars(sa.select(Note).where(Note.id == note_id))
            ).one_or_none()
            assert row is not None, "note should still be mirrored"
            # The path should reflect the move.
            assert row.path == "Templates/original.md"

    @pytest.mark.asyncio
    async def test_exclusion_problems_in_get_problems(self, store):
        """Excluded files appear in get_problems with kind 'excluded'."""
        _apply_settings(
            store,
            excluded_patterns=["*.excalidraw.md"],
        )

        root = store.notes_root
        _write_note_file(root, "Review/drawing.excalidraw.md", new_id(), {"type": "note"})

        from coppermind_store.sources import get_problems as store_get_problems

        await reconcile_once(store, full=True)
        problems = await store_get_problems(store)

        excluded_problems = [p for p in problems if p.kind == "excluded"]
        assert len(excluded_problems) >= 1
        paths = {p.reference for p in excluded_problems}
        assert "Review/drawing.excalidraw.md" in paths

    @pytest.mark.asyncio
    async def test_exclusion_count_in_status(self, store):
        """The status counters include excluded_count."""
        _apply_settings(
            store,
            excluded_patterns=["*.excalidraw.md"],
        )

        root = store.notes_root
        _write_note_file(root, "Review/drawing.excalidraw.md", new_id(), {"type": "note"})

        from coppermind_store.sources import get_status as store_get_status

        await reconcile_once(store, full=True)
        status = await store_get_status(store)

        assert status.counters.excluded_count >= 1

    @pytest.mark.asyncio
    async def test_exclusion_settings_applied_immediately(self, store):
        """A change to the setting takes effect on the next reconciler pass."""
        # First pass with no exclusions.
        _apply_settings(
            store,
            excluded_patterns=[],
        )

        _write_note_file(store.notes_root, "Review/normal.md", new_id(), {"type": "note"})
        await reconcile_once(store, full=True)

        # Should have 0 exclusions (empty pattern list).
        count1 = store.control.read_reconciler_excluded_count()
        assert count1 == 0

        # Now update the settings to exclude *.excalidraw.md.
        _apply_settings(
            store,
            excluded_patterns=["*.excalidraw.md"],
        )

        # Write a file matching the new exclusion.
        _write_note_file(
            store.notes_root, "Review/drawing.excalidraw.md", new_id(), {"type": "note"}
        )

        # Second pass - the new setting should take effect.
        await reconcile_once(store, full=True)

        count2 = store.control.read_reconciler_excluded_count()
        assert count2 >= 1
        exclusions = store.control.read_reconciler_exclusions()
        assert "Review/drawing.excalidraw.md" in exclusions
