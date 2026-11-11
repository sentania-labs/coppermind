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


def test_consent_review_changeover_and_deletion():
    store = VoiceStore(Context())
    old = {"model": "old", "version": "1", "vector": [1, 0]}
    new = {"model": "new", "version": "2", "vector": [0, 1]}
    store.confirm("alice", "note1", "2026-01-01", 25, [old])
    assert store.summary("alice") == {"enabled": False, "signatures": []}
    store.set_enabled("alice", True)
    store.confirm("alice", "note1", "2026-01-01", 25, [old, new])
    store.confirm("alice", "note1", "2026-01-01", 25, [old, new])
    assert len(store.summary("alice")["signatures"]) == 2
    assert store.match([old])[0]["person_id"] == "alice"
    assert store.match([dict(old, version="different")]) == []
    assert "vector" not in repr(store.summary("alice"))
    store.set_enabled("alice", False)
    assert store.match([new]) == []
    store.delete("alice")
    store.set_enabled("alice", True)
    assert store.match([new]) == []


def test_purge_removes_person_and_note_signatures():
    store = VoiceStore(Context())
    store.set_enabled("alice", True)
    store.set_enabled("bob", True)
    signature = {"model": "m", "version": "v", "vector": [1, 0]}
    store.confirm("alice", "note1", "today", 30, [signature])
    store.confirm("bob", "note2", "today", 30, [signature])
    store.purge({"alice"}, {"note2"})
    assert store.summary("alice")["enabled"] is False
    assert store.summary("bob")["signatures"] == []


def test_review_suggestions_never_expose_vectors_and_ignore_disabled_people():
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
    voice.set_enabled("alice", True)
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
    voice.set_enabled("alice", False)
    assert voice_suggestions(context, "note") == {}


def test_contradicted_pair_is_suppressed_and_removed_on_purge():
    store = VoiceStore(Context())
    store.reject("note", "Speaker 1", "alice")
    assert store.rejected("note", "Speaker 1", "alice")
    assert not store.rejected("other-note", "Speaker 1", "alice")
    store.purge(set(), {"note"})
    assert not store.rejected("note", "Speaker 1", "alice")
