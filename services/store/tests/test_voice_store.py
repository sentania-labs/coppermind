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
    context.recordings = SimpleNamespace(jobs=lambda: [SimpleNamespace(id="job", note_id="note")])
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
