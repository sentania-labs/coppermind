from copy import deepcopy
from typing import Any

from coppermind_store.voice_store import VoiceStore


class Context:
    def __init__(self):
        self.data = {}
        self.recordings: Any = None
        self.store: Any = None

    def read(self, name, default):
        return deepcopy(self.data.get(name, default))

    def write(self, name, value):
        self.data[name] = deepcopy(value)


def test_review_changeover_and_deletion():
    store = VoiceStore(Context())
    old = {"model": "old", "version": "1", "vector": [1, 0]}
    new = {"model": "new", "version": "2", "vector": [0, 1]}
    store.confirm("alice", "note1", "2026-01-01", 25, [old])
    assert len(store.summary("alice")["signatures"]) == 1
    store.confirm("alice", "note1", "2026-01-01", 25, [old, new])
    store.confirm("alice", "note1", "2026-01-01", 25, [old, new])
    assert len(store.summary("alice")["signatures"]) == 2
    assert store.match([old])[0]["person_id"] == "alice"
    assert store.match([dict(old, version="different")]) == []
    assert "vector" not in repr(store.summary("alice"))
    store.delete("alice")
    assert store.summary("alice") == {"signatures": [], "assignments": []}
    assert store.match([new]) == []


def test_deleting_a_person_removes_their_signatures_and_turned_down_suggestions():
    store = VoiceStore(Context())
    signature = {"model": "m", "version": "v", "vector": [1, 0]}
    store.confirm("alice", "note1", "today", 30, [signature])
    store.confirm("bob", "note1", "today", 30, [signature])
    store.reject("note2", "Speaker 1", "alice")
    store.delete("alice")
    assert store.summary("alice") == {"signatures": [], "assignments": []}
    assert not store.rejected("note2", "Speaker 1", "alice")
    assert store.match([signature])[0]["person_id"] == "bob"
    assert len(store.summary("bob")["signatures"]) == 1


def test_purge_removes_person_and_note_signatures():
    store = VoiceStore(Context())
    signature = {"model": "m", "version": "v", "vector": [1, 0]}
    store.confirm("alice", "note1", "today", 30, [signature])
    store.confirm("bob", "note2", "today", 30, [signature])
    store.purge({"alice"}, {"note2"})
    assert store.summary("alice")["signatures"] == []
    assert store.summary("bob")["signatures"] == []


def test_review_suggestions_never_expose_vectors_and_stop_after_deletion():
    from types import SimpleNamespace

    from coppermind_store.speaker_names import voice_suggestions

    context = Context()
    context.recordings = SimpleNamespace(
        jobs=lambda: [SimpleNamespace(id="job", note_id="note", source_id="src")]
    )
    alice = SimpleNamespace(
        id="alice",
        kind="person",
        name="Alice",
        emails=[],
        path="People/Alice.md",
        retired_at=None,
        redirect=None,
    )
    context.store = SimpleNamespace(
        entities=SimpleNamespace(load=lambda: SimpleNamespace(entities={"alice": alice}))
    )
    voice = VoiceStore(context)
    signature = {"model": "m", "version": "v", "vector": [1, 0]}
    voice.confirm("alice", "old-note", "today", 30, [signature])
    context.write(
        "voice-pending/job",
        [
            {"label": "Speaker 1", "embeddings": [signature]},
            {"label": "Speaker 2", "embeddings": []},
        ],
    )
    suggestion = voice_suggestions(context, "note")
    assert suggestion["Speaker 1"][0]["name"] == "Alice"
    assert suggestion["Speaker 1"][0]["strength"] == "strong"
    assert "vector" not in repr(suggestion)
    voice.delete("alice")
    assert voice_suggestions(context, "note") == {}


def test_contradicted_pair_is_suppressed_and_removed_on_purge():
    store = VoiceStore(Context())
    store.reject("note", "Speaker 1", "alice")
    assert store.rejected("note", "Speaker 1", "alice")
    assert not store.rejected("other-note", "Speaker 1", "alice")
    store.purge(set(), {"note"})
    assert not store.rejected("note", "Speaker 1", "alice")


def test_unknown_voice_is_stable_across_recordings_and_private():
    store = VoiceStore(Context())
    signature = {"model": "m", "version": "1", "vector": [1, 0]}
    first = store.capture_unknown("note1", "Speaker 1", "today", 10, [signature])
    assert first == "unknown:1"
    assert store.capture_unknown("note1", "Speaker 1", "today", 10, [signature]) == first
    assert store.capture_unknown("note2", "Speaker 2", "today", 10, [signature]) == first
    assert store.capture_unknown("note2", "Speaker 3", "today", 10, [signature]) != first
    assert len(store.unknown_summaries()[first]["signatures"]) == 2
    assert "vector" not in repr(store.unknown_summaries())


def test_unknown_capture_respects_global_switch():
    context = Context()
    context.write("voice-signatures", {"people": {}, "recognition_enabled": False})
    store = VoiceStore(context)
    signature = {"model": "m", "version": "1", "vector": [1, 0]}
    assert store.capture_unknown("note", "Speaker 1", "today", 10, [signature]) is None
    assert store.unknown_summaries() == {}


def test_speaker_without_embedding_has_no_voice_identity():
    store = VoiceStore(Context())
    assert store.capture_unknown("note1", "Speaker 1", "today", 3, []) is None
    assert store.unknown_summaries() == {}
    assert store._load().get("assignments", []) == []
    store.confirm("alice", "note1", "today", 3, [])
    assert store._load()["people"] == {}


def test_move_and_split_keep_recording_assignments_and_signatures():
    store = VoiceStore(Context())
    signature = {"model": "m", "version": "1", "vector": [1, 0]}
    voice = store.capture_unknown("recording:job", "Speaker 1", "today", 10, [signature])
    store.bind_recording("recording:job", "note1")
    store.capture_unknown("note2", "Speaker 2", "today", 10, [signature])
    assert len(store.move_voice(voice, "person:alice")) == 2
    assert len(store.summary("alice")["signatures"]) == 2
    separated = store.split_assignment("person:alice", "note1", "Speaker 1")
    assert len(store.assignments(separated)) == 1
    assert len(store.assignments("person:alice")) == 1
    assert len(store.summary("alice")["signatures"]) == 1
    assert store.unknown_summaries()[separated]["signatures"][0]["note_id"] == "note1"


def test_relabel_preserves_edits_and_only_changes_assigned_turns():
    from coppermind_store.voice_relabel import relabel_generated_lines, relabel_transcript

    transcript = {
        "segments": [
            {"speaker": "Morgan", "text": "first"},
            {"speaker": "Morgan", "text": "second"},
        ]
    }
    result = relabel_transcript(transcript, [0], "Morgan", "Unknown voice 2")
    assert [s["speaker"] for s in result["segments"]] == ["Unknown voice 2", "Morgan"]
    body = "Morgan: generated\nMorgan: owner edit\nMorgan: duplicated\nMorgan: duplicated\n"
    generated = "Morgan: generated\nMorgan: original\nMorgan: duplicated\n"
    body, _, generated = relabel_generated_lines(body, body, generated, 0, "Morgan", "Alex")
    assert body == "Alex: generated\nMorgan: owner edit\nMorgan: duplicated\nMorgan: duplicated\n"
    assert generated.startswith("Alex: generated\n")


def test_relabel_skips_turns_already_renamed_elsewhere():
    from coppermind_store.voice_relabel import relabel_transcript

    transcript = {
        "segments": [
            {"speaker": "Alice Smith", "text": "first"},
            {"speaker": "Unknown voice 3", "text": "second"},
        ]
    }
    result = relabel_transcript(transcript, [0, 1], "Unknown voice 3", "Bob")
    assert [s["speaker"] for s in result["segments"]] == ["Alice Smith", "Bob"]


def merged_pair():
    store = VoiceStore(Context())
    vector = {"model": "m", "version": "v", "vector": [1, 0]}
    first = store.capture_unknown("recording:a", "Speaker 1", "today", 30, [vector])
    second = store.capture_unknown("recording:b", "Speaker 1", "today", 30, [vector])
    assert first == second
    store.reject("recording:a", "Speaker 1", "alice")
    store.reject("recording:b", "Speaker 1", "alice")
    store.reparent_recording("recording:a", "dest", "src-a")
    store.reparent_recording("recording:b", "dest", "src-b")
    return store, first


def test_split_on_one_merged_recording_leaves_the_other_intact():
    store, voice = merged_pair()
    new = store.split_assignment(voice, "dest", "Speaker 1", "src-a")
    data = store._load()
    assert [s["source_id"] for s in data["unknowns"][new]["signatures"]] == ["src-a"]
    assert [s["source_id"] for s in data["unknowns"][voice]["signatures"]] == ["src-b"]
    assert [(a["voice_id"], a["source_id"]) for a in data["assignments"]] == [
        (new, "src-a"),
        (voice, "src-b"),
    ]
    store.restore_unknown(new)
    assert [s["source_id"] for s in store._load()["unknowns"][voice]["signatures"]] == ["src-b"]


def test_restore_on_one_merged_recording_leaves_the_other_intact():
    store, voice = merged_pair()
    new = store.split_assignment(voice, "dest", "Speaker 1", "src-a")
    store.move_voice(new, voice)
    store.delete_unknown(voice)
    assert store._load()["assignments"] == []
    store, voice = merged_pair()
    new = store.split_assignment(voice, "dest", "Speaker 1", "src-a")
    store.move_voice(new, voice)
    store.restore_unknown(new)
    data = store._load()
    assert [s["source_id"] for s in data["unknowns"][new]["signatures"]] == ["src-a"]
    assert [s["source_id"] for s in data["unknowns"][voice]["signatures"]] == ["src-b"]


def test_assignment_without_its_recording_is_not_found():
    store, voice = merged_pair()
    try:
        store.split_assignment(voice, "dest", "Speaker 1")
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_rejection_on_one_merged_recording_does_not_reject_the_other():
    store = VoiceStore(Context())
    store.reject("dest", "Speaker 1", "alice", "src-a")
    assert store.rejected("dest", "Speaker 1", "alice", "src-a")
    assert not store.rejected("dest", "Speaker 1", "alice", "src-b")


def test_deferred_rename_is_kept_per_merged_recording():
    store, voice = merged_pair()
    for source in ("src-a", "src-b"):
        assignment = next(a for a in store.assignments(voice) if a["source_id"] == source)
        store.defer_relabel(assignment, source)
    assert {d["name"] for d in store.deferred_relabels()} == {"src-a", "src-b"}
    store.clear_deferred("dest", "Speaker 1", "src-a")
    assert [d["name"] for d in store.deferred_relabels()] == ["src-b"]


def test_confirming_each_merged_recording_keeps_its_own_signature():
    store = VoiceStore(Context())
    vector = {"model": "m", "version": "v", "vector": [1, 0]}
    store.confirm("alice", "dest", "today", 30, [vector], "src-a")
    store.confirm("alice", "dest", "today", 30, [vector], "src-b")
    store.confirm("alice", "dest", "today", 30, [vector], "src-b")
    assert len(store.summary("alice")["signatures"]) == 2


def test_older_merged_rows_are_attributed_to_their_one_recording():
    context = Context()
    context.data["voice-signatures"] = {
        "people": {},
        "next_unknown": 2,
        "unknowns": {
            "unknown:1": {
                "name": "Unknown voice 1",
                "signatures": [{"note_id": "dest", "label": "Speaker 1", "vector": [1, 0]}],
            }
        },
        "assignments": [
            {
                "note_id": "dest",
                "label": "Speaker 1",
                "voice_id": "unknown:1",
                "source_id": "src-a",
            }
        ],
        "rejections": [{"note_id": "dest", "label": "Speaker 1", "person_id": "alice"}],
    }
    store = VoiceStore(context)
    assert store.rejected("dest", "Speaker 1", "alice", "src-a")
    assert not store.rejected("dest", "Speaker 1", "alice", "src-b")
    store.split_assignment("unknown:1", "dest", "Speaker 1", "src-a")
    assert store._load()["unknowns"]["unknown:1"]["signatures"] == []
