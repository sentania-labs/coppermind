"""Real reconciliation preserves excluded bytes and follows identities after filing."""

from pathlib import Path

import sqlalchemy as sa
from coppermind_store.control import ControlState
from coppermind_store.reconciler import UnidentifiedStats, reconcile_once

from coppermind.db.models import Note
from coppermind.store_protocol import CreateNote


def write(root: Path, relative: str, content: str = "# Device note\n") -> bytes:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path.read_bytes()


async def test_exclusions_are_not_adopted_and_counts_survive_ordinary_passes(store):
    files = {
        "Templates/a.md": "# Template\n",
        "_Templates/nested/a.md": "# Template\n",
        "drawing.excalidraw.md": "# Drawing\n",
        "board.kanban.md": "# Board\n",
        "sidecar.canvas": '{"nodes": [], "edges": []}',
        "drawing.md": "---\nexcalidraw-plugin: parsed\n---\n",
        "board.md": "---\nkanban-plugin: basic\n---\n",
    }
    before = {path: write(store.notes_root, path, content) for path, content in files.items()}
    write(store.notes_root, "ordinary.md")
    remembered: UnidentifiedStats = {}
    for expected_adopted in (1, 0):
        counts = await reconcile_once(store, unidentified=remembered)
        assert counts["adopted"] == expected_adopted
        assert counts["excluded"] == len(files)
        assert (await store.get_status()).counters.excluded_count == len(files)
        problems = {
            p.reference: p.reason for p in await store.get_problems() if p.kind == "excluded"
        }
        assert problems == {
            "Templates/a.md": "folder: Templates/",
            "_Templates/nested/a.md": "folder: _Templates/",
            "drawing.excalidraw.md": "pattern: *.excalidraw.md",
            "board.kanban.md": "pattern: *.kanban.md",
            "sidecar.canvas": "pattern: *.canvas",
            "drawing.md": "frontmatter: excalidraw-plugin",
            "board.md": "frontmatter: kanban-plugin",
        }
    assert {path: (store.notes_root / path).read_bytes() for path in files} == before
    async with store.session_factory() as session:
        rows = (await session.scalars(sa.select(Note))).all()
        assert [row.path for row in rows] == ["ordinary.md"]
    # The snapshot survives a process restart and clears when files disappear.
    assert ControlState(store.control.store.state_dir).read_reconciler_excluded_count() == len(
        files
    )
    for path in files:
        (store.notes_root / path).unlink()
    assert (await reconcile_once(store, unidentified=remembered))["excluded"] == 0
    assert not [p for p in await store.get_problems() if p.kind == "excluded"]


async def test_known_identity_filed_into_excluded_places_is_never_missing(store):
    note = await store.create_note(CreateNote(title="File me", body="# File me\n"))
    for relative in (
        "Templates/file.md",
        "_Templates/file.md",
        "file.excalidraw.md",
        "file.canvas",
    ):
        current = await store.get_note(note.id)
        target = store.notes_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        (store.notes_root / current.path).rename(target)
        counts = await reconcile_once(store)
        assert counts["missing"] == 0
        assert counts["excluded"] == 0
        current = await store.get_note(note.id)
        assert current.path == relative
        async with store.session_factory() as session:
            row = await session.get(Note, note.id)
            assert row is not None and row.state == "ok"


async def test_custom_rules_reload_on_next_pass_without_restart_or_file_edit(store):
    path = "Archive/file.tmp.md"
    original = write(store.notes_root, path)
    state = store.control.store
    settings = store.control.settings()
    settings.reconcile.excluded_patterns = ["*.tmp.md"]
    state.write("settings", settings.model_dump(), if_revision=state.read("settings").revision)
    remembered: UnidentifiedStats = {}
    assert (await reconcile_once(store, unidentified=remembered))["excluded"] == 1
    assert (store.notes_root / path).read_bytes() == original
    settings.reconcile.excluded_patterns = []
    settings.reconcile.excluded_folders = ["Archive/"]
    state.write("settings", settings.model_dump(), if_revision=state.read("settings").revision)
    assert (await reconcile_once(store, unidentified=remembered))["excluded"] == 1
    assert store.control.read_reconciler_exclusions() == {path: "folder: Archive/"}
    settings.reconcile.excluded_folders = []
    state.write("settings", settings.model_dump(), if_revision=state.read("settings").revision)
    counts = await reconcile_once(store, unidentified=remembered)
    assert counts["excluded"] == 0
    assert counts["adopted"] == 1
    assert (store.notes_root / path).read_bytes() != original
