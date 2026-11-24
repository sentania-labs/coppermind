"""Delete means gone: every recovery copy and deleted-item record is dropped in this pass.

Before this revision a deleted note left three kinds of record behind: its mirror row
(`state` `deleted` or `missing`), a recovery receipt under `state/note-recovery`, and the
bytes Admin itself took away under `state/admin-removed`. Item 1 of issue 143 makes a
deletion answer once, so all three go in this one upgrade. The vault's Git history is the
safety net from here on and is never written by a migration.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-23
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _state_dir() -> Path:
    from coppermind.settings import Wiring

    return Wiring().state_dir


def _unlinked(path: Path) -> int:
    with suppress(OSError):
        path.unlink(missing_ok=True)
    return 1


def upgrade() -> None:
    # One pass, no keep time: the old rows are the recovery copies themselves.
    op.execute(
        "DELETE FROM note_sources WHERE note_id IN "
        "(SELECT id FROM notes WHERE state IN ('deleted', 'missing'))"
    )
    op.execute("DELETE FROM notes WHERE state IN ('deleted', 'missing')")

    from coppermind.statefiles import StateStore

    state = StateStore(_state_dir())
    # The count and receipts are derived from the rows just dropped, so they go with them.
    for name in ("cleanup_count", "purge-problems"):
        with suppress(OSError):
            state.state_dir.joinpath(name + ".json").unlink(missing_ok=True)
    # Receipts and Admin's own removed copies were the old second safety net.
    for folder in ("note-recovery", "admin-removed"):
        root = state.state_dir / folder
        with suppress(OSError):
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    _unlinked(path)
            for path in sorted(root.rglob("*"), reverse=True):
                with suppress(OSError):
                    path.rmdir()


def downgrade() -> None:
    # The receipts and rows were dropped by design; a later scan rebuilds what still exists.
    pass
