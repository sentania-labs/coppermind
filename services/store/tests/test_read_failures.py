from pathlib import Path
from typing import Any, cast

import pytest
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore
from structlog.testing import capture_logs

from coppermind.settings import Wiring
from coppermind.store_protocol import NotesFilesystemUnavailable, NoteUnparseable, NotFound

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
OTHER_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F3"
NOTE_BYTES = b"---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\nsources: []\n---\n# Runbook\n"


def local_store(tmp_path: Path) -> LocalStore:
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    return LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)


async def test_a_file_that_vanishes_after_location_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = local_store(tmp_path)
    missing = tmp_path / "vanished.md"

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/vanished.md", missing

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NotFound) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID


async def test_a_file_metadata_read_failure_reports_the_notes_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = local_store(tmp_path)

    class StatFailurePath:
        def read_bytes(self) -> bytes:
            return NOTE_BYTES

        def stat(self):
            raise PermissionError("metadata read denied")

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", cast(Path, StatFailurePath())

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NotesFilesystemUnavailable) as raised:
        await store.get_note(NOTE_ID)
    assert "metadata read denied" in str(raised.value)


async def test_a_file_carrying_another_identifier_is_never_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """One note deleted on a device and another renamed onto its path.

    Nothing reconciles the mirror yet, so the row still names that path.
    Serving what is there would answer a different note under the requested
    identifier, which is worse than a miss.
    """
    store = local_store(tmp_path)
    renamed = tmp_path / "Runbook.md"
    renamed.write_bytes(NOTE_BYTES.replace(NOTE_ID.encode(), OTHER_ID.encode()))

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", renamed

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NotFound) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID


async def test_a_file_without_an_identifier_still_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A note a person created in Obsidian carries no id key of its own yet."""
    store = local_store(tmp_path)
    hand_written = tmp_path / "Runbook.md"
    hand_written.write_bytes(b"---\nsources: []\n---\n# Runbook\n")

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", hand_written

    monkeypatch.setattr(store, "_locate", locate)

    fetched = await store.get_note(NOTE_ID)
    assert fetched.id == NOTE_ID
    assert fetched.path == "Review/Runbook.md"
    assert fetched.body.startswith("# Runbook")


BROKEN_NOTES = {
    "duplicate_key": (
        "---\naccount: AcmeCorp\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n"
        "account: SecretMerger\n---\n# Runbook\n",
        ["AcmeCorp", "SecretMerger", "account"],
    ),
    "unterminated_sequence": (
        "---\ntags: [unclosed\nsalary_band: L7\n---\n# Runbook\n",
        ["unclosed", "salary_band", "L7"],
    ),
}


@pytest.mark.parametrize("case", sorted(BROKEN_NOTES))
async def test_a_broken_note_never_puts_its_own_text_in_the_log(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Logs are collected and shipped, so note content cannot enter them.

    The parser's reason quotes the lines it choked on, so it is the person's
    own note content. A duplicate key is the case that catches a plausible but
    wrong version of this: the parser names the key and both of its values.

    The assertions are on the absence of that text rather than on the presence
    of a particular category, so renaming a category later cannot make this
    test pass while the content leaks again. What the operator needs to open
    the file, the identifier and the path, is asserted present.
    """
    text, secrets = BROKEN_NOTES[case]
    store = local_store(tmp_path)
    broken = tmp_path / "Runbook.md"
    broken.write_text(text, encoding="utf-8")

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", broken

    monkeypatch.setattr(store, "_locate", locate)

    with capture_logs() as captured, pytest.raises(NoteUnparseable) as raised:
        await store.get_note(NOTE_ID)

    logged = repr(captured)
    for secret in secrets:
        assert secret not in logged
    assert str(raised.value.reason) not in logged
    assert "<unicode string>" not in logged
    assert NOTE_ID in logged
    assert "Review/Runbook.md" in logged
    # The internal surface, which the store alone answers, still gets it all.
    for secret in secrets:
        assert secret in raised.value.reason


# --- New tests for the identity-first recovery on parse failure ---


async def test_broken_frontmatter_carrying_another_id_raises_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A file on disk cannot parse but carries a different note's identifier.

    The store tries to recover the id from the raw frontmatter block before
    announcing a parse failure. When the recovered id does not match the
    requested identifier, the honest answer is a miss, not an unparseable
    file belonging to the requested note.
    """
    store = local_store(tmp_path)
    broken = tmp_path / "Runbook.md"
    broken.write_text(
        "---\naccount: AcmeCorp\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F3\n"
        "account: SecretMerger\n---\n# Runbook\n",
        encoding="utf-8",
    )

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", broken

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NotFound) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID


async def test_broken_frontmatter_carrying_its_own_id_raises_unparseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A file on disk cannot parse and carries the requested identifier.

    The store recovers the id from the raw block, finds it matches the
    requested note, and so raises ``NoteUnparseable`` rather than hiding the
    broken file behind a ``NotFound``.
    """
    store = local_store(tmp_path)
    broken = tmp_path / "Runbook.md"
    broken.write_text(
        "---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n"
        "account: AcmeCorp\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n"
        "---\n# Runbook\n",
        encoding="utf-8",
    )

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", broken

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NoteUnparseable) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID


async def test_broken_frontmatter_with_no_recoverable_id_raises_unparseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A file on disk cannot parse and carries no recoverable identifier.

    Without an id to recover, the store falls back to the ordinary
    ``NoteUnparseable`` answer.
    """
    store = local_store(tmp_path)
    broken = tmp_path / "Runbook.md"
    broken.write_text(
        "---\ntags: [unclosed\nsalary_band: L7\n---\n# Runbook\n",
        encoding="utf-8",
    )

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", broken

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NoteUnparseable) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID


async def test_undecodable_bytes_still_answer_unparseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Undecodable bytes do not cause a UnicodeDecodeError in the error path.

    The original except handler decoded data again, which would re-raise
    UnicodeDecodeError when the parse failure was caused by invalid bytes.
    The fix only attempts id recovery for FrontmatterError, so a file with
    invalid UTF-8 in its body still answers NoteUnparseable cleanly.
    """
    store = local_store(tmp_path)
    broken = tmp_path / "Runbook.md"
    broken.write_bytes(b"---\nid: x\n---\n\xff\xfe")

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", broken

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NoteUnparseable) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID
    assert raised.value.reason is not None
