"""Marking a note reviewed always files it: filing needs only a day.

No start time, context or account is asked for, a name already taken adds " (2)" and nothing
is ever overwritten, notes held by the old rules file on the next pass, and a real failure is a
row on Notes in Review with Try again.
"""

import json
import time as clock

import pytest
from coppermind_store import filing, note_lifecycle
from coppermind_store.reconciler import reconcile_once

from coppermind.ids import new_id
from coppermind.schema import WORK_ONLY_CONTEXT_KEY, shipped_key_upgrades
from coppermind.store_protocol import NoteQuery, NotesFilesystemUnavailable


def hand_written(store, name: str, front: str, folder: str = "Review", body: str = "Words.\n"):
    note_id = new_id()
    directory = store.notes_root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nid: {note_id}\nschema_version: 1\nsources: []\n{front}---\n# {name}\n\n{body}",
        encoding="utf-8",
    )
    return note_id


async def file_everything(store, monkeypatch) -> dict[str, int]:
    """One reconcile and one filing pass, with the reviewed files long since settled."""
    await reconcile_once(store)
    stable = clock.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: stable)
    return await filing.file_reviewed(store)


def rule(control, name: str):
    return next(k for k in control.schema().keys if k.name == name).required_when


async def path_of(store, note_id: str) -> str:
    return (await store.get_note(note_id)).path


async def test_a_reviewed_work_note_with_only_a_day_files(store, monkeypatch):
    note_id = hand_written(store, "Hallway chat", "type: work\nreviewed: true\ndate: 2026-11-26\n")
    assert await file_everything(store, monkeypatch) == {"filed": 1, "filing_failed": 0}
    assert (await path_of(store, note_id)).startswith("work/2026/2026-11/")


async def test_a_reviewed_note_with_no_context_or_account_files(store, monkeypatch):
    bare = hand_written(store, "Bare", "type: work\nreviewed: true\ndate: 2026-10-20\n")
    customer = hand_written(
        store, "Customer", "type: work\ncontext: external\nreviewed: true\ndate: 2026-10-21\n"
    )
    assert await file_everything(store, monkeypatch) == {"filed": 2, "filing_failed": 0}
    assert (await path_of(store, bare)).startswith("work/2026/2026-10/")
    assert (await path_of(store, customer)).startswith("work/2026/2026-10/")


async def test_a_stray_time_unknown_is_tolerated_and_left_as_it_is(store, monkeypatch):
    note_id = hand_written(
        store, "Old", "type: work\nreviewed: true\ndate: 2026-10-20\ntime_unknown: true\n"
    )
    assert await file_everything(store, monkeypatch) == {"filed": 1, "filing_failed": 0}
    filed = await store.get_note(note_id)
    assert filed.path.startswith("work/2026/2026-10/")
    assert filed.frontmatter["time_unknown"] is True


async def test_a_name_already_taken_adds_a_number_and_nothing_is_overwritten(store, monkeypatch):
    taken = hand_written(
        store,
        "2026-10-20 Budget",
        "type: work\nreviewed: true\ndate: 2026-10-20\n",
        "work/2026/2026-10",
    )
    other = hand_written(
        store, "2026-10-20 Budget", "type: work\nreviewed: true\ndate: 2026-10-20\n"
    )
    before = (store.notes_root / "work/2026/2026-10/2026-10-20 Budget.md").read_bytes()
    # The note already in the folder is filed where it stands, and the new one beside it.
    assert await file_everything(store, monkeypatch) == {"filed": 2, "filing_failed": 0}
    assert await path_of(store, taken) == "work/2026/2026-10/2026-10-20 Budget.md"
    assert await path_of(store, other) == "work/2026/2026-10/2026-10-20 Budget (2).md"
    assert (store.notes_root / "work/2026/2026-10/2026-10-20 Budget.md").read_bytes() == before
    assert (
        "# 2026-10-20 Budget\n"
        in (store.notes_root / "work/2026/2026-10/2026-10-20 Budget (2).md").read_text()
    )

    third = hand_written(
        store, "2026-10-20 Budget", "type: work\nreviewed: true\ndate: 2026-10-20\n"
    )
    assert await file_everything(store, monkeypatch) == {"filed": 1, "filing_failed": 0}
    assert await path_of(store, third) == "work/2026/2026-10/2026-10-20 Budget (3).md"
    assert not (store.notes_root / "Review/2026-10-20 Budget.md").exists()


async def test_notes_held_by_the_old_rules_file_on_the_next_pass_after_an_upgrade(
    store, control, monkeypatch
):
    # An install from before: the saved schema still says a customer note needs an account.
    upgrades = shipped_key_upgrades()
    state = control.store.read("schema")
    body = {k: v for k, v in state.body.items() if k != "revision"}
    old = {"context": WORK_ONLY_CONTEXT_KEY, "account": upgrades["account"][0][1]}
    body["keys"] = [
        old[key["name"]].model_dump(mode="json") if key["name"] in old else key
        for key in body["keys"]
    ]
    control.store.write("schema", body, if_revision=state.revision)
    assert rule(control, "context") == "type" and rule(control, "account") == "context"

    # A customer note with no account: refused, so it never became a note.
    customer = hand_written(
        store, "Customer", "type: work\ncontext: external\nreviewed: true\ndate: 2026-10-21\n"
    )
    # A work note with no start time that an earlier release held, with its recorded reason.
    no_time = hand_written(store, "No time", "type: work\nreviewed: true\ndate: 2026-10-22\n")
    # A note held because a file with its name was already in the folder.
    clash = hand_written(
        store, "2026-10-23 Clash", "type: work\nreviewed: true\ndate: 2026-10-23\n"
    )
    hand_written(
        store,
        "2026-10-23 Clash",
        "type: work\nreviewed: true\ndate: 2026-10-23\n",
        "work/2026/2026-10",
    )
    await reconcile_once(store)
    held = {
        no_time: "ValueError: confirm the meeting time, or select Time unknown",
        clash: "PathCollision",
    }
    for note_id, error in held.items():
        journal = store.control.store.state_dir / "filing" / f"{note_id}.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({"note_id": note_id, "attempts": 3, "error": error}))
    assert customer not in {n.id for n in (await store.list_notes(NoteQuery())).items}

    control.ensure_defaults()  # the upgrade
    assert rule(control, "context") is None and rule(control, "account") is None

    counts = await file_everything(store, monkeypatch)
    # The three held notes, and the older "2026-10-23 Clash" already sitting in its folder.
    assert counts == {"filed": 4, "filing_failed": 0}
    for note_id in (customer, no_time, clash):
        assert not (await path_of(store, note_id)).startswith("Review/")
    assert await path_of(store, clash) == "work/2026/2026-10/2026-10-23 Clash (2).md"


async def test_a_real_failure_is_a_row_with_one_sentence_and_try_again_files_it(store, monkeypatch):
    note_id = hand_written(store, "Hallway chat", "type: work\nreviewed: true\ndate: 2026-11-26\n")
    review = store.context.review_notes
    real_move = type(store).move_note

    async def disk_full(self, *args, **kwargs):
        raise NotesFilesystemUnavailable("no space left on device")

    monkeypatch.setattr(type(store), "move_note", disk_full)
    assert await file_everything(store, monkeypatch) == {"filed": 0, "filing_failed": 1}
    row = next(n for n in (await review.listing({}))["notes"] if n["id"] == note_id)
    assert row["filing"] == {
        "problem": "This note could not be filed because its folder could not be written to."
    }
    # Still failing: Try again says so, in the same sentence.
    again = await review.file_again({"id": note_id})
    assert again == {"filed": False, "problem": row["filing"]["problem"]}
    assert (await path_of(store, note_id)).startswith("Review/")

    monkeypatch.setattr(type(store), "move_note", real_move)
    assert await review.file_again({"id": note_id}) == {"filed": True, "problem": ""}
    assert (await path_of(store, note_id)).startswith("work/2026/2026-11/")
    assert note_id not in {n["id"] for n in (await review.listing({}))["notes"]}


async def test_a_note_about_to_be_filed_or_not_yet_reviewed_is_not_a_filing_row(store, monkeypatch):
    waiting = hand_written(store, "Waiting", "type: work\nreviewed: false\ndate: 2026-11-26\n")
    settling = hand_written(store, "Settling", "type: work\nreviewed: true\ndate: 2026-11-26\n")
    await reconcile_once(store)
    journal = store.control.store.state_dir / "filing" / f"{settling}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"note_id": settling, "error": note_lifecycle.SETTLING}))
    # A note an earlier release held for its start time is waiting for the next pass, not failed.
    retired = hand_written(store, "Retired", "type: work\nreviewed: true\ndate: 2026-11-26\n")
    await reconcile_once(store)
    journal = store.control.store.state_dir / "filing" / f"{retired}.json"
    journal.write_text(
        json.dumps({"note_id": retired, "error": "ValueError: confirm the meeting time, or x"})
    )
    rows = {n["id"]: n for n in (await store.context.review_notes.listing({}))["notes"]}
    assert "filing" not in rows[waiting] and "filing" not in rows[settling]
    assert "filing" not in rows[retired]


@pytest.mark.parametrize(
    ("error", "sentence"),
    [
        ("SourceRevisionNeedsReview", "its recording changed after you reviewed it"),
        ("ValueError: a note date is required in date", "it has no date Coppermind can read"),
        ("NoteUnparseable", "Coppermind could not read it"),
        ("PermissionError", "its folder could not be written to"),
        ("MetadataUnavailable", "Coppermind's records were busy"),
    ],
)
def test_each_failure_is_one_plain_sentence(error, sentence):
    from coppermind_store.review_notes import filing_sentence

    assert filing_sentence(error) == f"This note could not be filed because {sentence}."
    assert filing_sentence("Odd") == "This note could not be filed."
