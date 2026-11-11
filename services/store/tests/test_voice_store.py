from copy import deepcopy

from coppermind_store.voice_store import VoiceStore


class Context:
    def __init__(self):
        self.data = {}

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
