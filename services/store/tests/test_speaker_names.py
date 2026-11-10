from coppermind_store.speaker_names import rename_speakers, speaker_labels


def test_renaming_only_changes_matching_segment_labels():
    transcript = {
        "segments": [
            {"speaker": "Speaker 1", "text": "Speaker 1 is mentioned."},
            {"speaker": "Speaker 10", "text": "Hello"},
            {"speaker": "Speaker 1", "text": "Again"},
        ]
    }
    assert speaker_labels(transcript) == ["Speaker 1", "Speaker 10"]
    renamed = rename_speakers(transcript, {"Speaker 1": "Alex Morgan"})
    assert [s["speaker"] for s in renamed["segments"]] == [
        "Alex Morgan",
        "Speaker 10",
        "Alex Morgan",
    ]
    assert renamed["segments"][0]["text"] == "Speaker 1 is mentioned."
    assert transcript["segments"][0]["speaker"] == "Speaker 1"


async def test_calendar_candidates_only_come_from_linked_events():
    import json
    from types import SimpleNamespace

    from coppermind_store.speaker_names import evidence

    transcript = {"segments": [{"start": 0, "speaker": "Speaker 1", "text": "Hello"}]}

    class Store:
        entities = SimpleNamespace(load=lambda: SimpleNamespace(entities={}))

        async def get_source(self, source_id):
            return SimpleNamespace(current_revision=1)

        async def get_source_artifact(self, source_id, revision, name):
            return SimpleNamespace(content=json.dumps(transcript))

    class Context:
        store = Store()
        recordings = SimpleNamespace(
            jobs=lambda: [SimpleNamespace(note_id="note", source_id="src")]
        )

        def config(self):
            return SimpleNamespace(assignments={"enrichment": SimpleNamespace(event_field="event")})

        def read(self, name, default):
            return default

    note = SimpleNamespace(id="note", frontmatter={"event": "linked"})
    events = [
        {
            "event_id": "linked",
            "people": [{"contact_id": "c1", "name": "Alex", "address": "mailto:alex@example.test"}],
        },
        {
            "event_id": "unrelated",
            "people": [
                {"contact_id": "c2", "name": "Blair", "address": "mailto:blair@example.test"}
            ],
        },
    ]
    data = await evidence(Context(), note, events)
    assert [c["name"] for c in data["candidates"]] == ["Alex"]
