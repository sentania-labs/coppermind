"""The companion cannot use note-only credentials to read source evidence."""

import pytest
from coppermind_api.auth import Principal
from coppermind_api.deps import authenticated_key, store
from coppermind_api.v1.workflow import router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coppermind.api_keys import PERMISSIONS


class Store:
    def __init__(self):
        self.calls = []

    async def context_action(self, action, payload):
        self.calls.append((action, payload))
        return {"ok": True}

    async def get_artifact_limit(self):
        return 1024


@pytest.mark.parametrize(
    "scopes,expected", [(["notes:read"], 403), (["notes:read", "sources:read"], 200)]
)
def test_review_evidence_scopes(scopes, expected):
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    with TestClient(app) as client:
        result = client.get("/v1/notes/note/review")
    assert result.status_code == expected
    assert len(fake.calls) == (expected == 200)


def test_review_write_requires_write_scope():
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal(
        "test", frozenset({"notes:read", "sources:read"})
    )
    with TestClient(app) as client:
        result = client.post(
            "/v1/notes/note/review", json={"if_match": "hash", "confirmation": "Complete review"}
        )
    assert result.status_code == 403
    assert fake.calls == []


@pytest.mark.parametrize(
    "scopes,kind,expected",
    [
        (["sources:write"], "artifact", 403),
        (["artifacts:write"], "artifact", 403),
        (["sources:write", "artifacts:write"], "artifact", 202),
        (["sources:write"], None, 202),
    ],
)
def test_a_drop_that_declares_a_kind_needs_the_artifact_write_scope(scopes, kind, expected):
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    params = {"filename": "deck.pptx", **({"kind": kind} if kind else {})}
    with TestClient(app) as client:
        result = client.post("/v1/dropbox", params=params, content=b"PK")
    assert result.status_code == expected
    assert [action for action, _ in fake.calls] == (["upload-drop"] if expected == 202 else [])


@pytest.mark.parametrize(
    "scopes,expected",
    [
        (["sources:write"], [{"artifact": "stored"}]),
        (
            ["sources:write", "artifacts:read"],
            [{"artifact_id": "A1", "path": "Artifacts/deck.pptx"}],
        ),
    ],
)
def test_an_already_imported_answer_names_the_artifact_only_with_artifact_read(scopes, expected):
    class Answering(Store):
        async def context_action(self, action, payload):
            return {
                "state": "already_imported",
                "parts": [{"artifact_id": "A1", "path": "Artifacts/deck.pptx"}],
            }

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: Answering()
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    with TestClient(app) as client:
        result = client.post("/v1/dropbox", params={"filename": "deck.pptx"}, content=b"PK")
    assert result.status_code == 202
    assert result.json()["parts"] == expected


def test_a_declared_drop_is_bounded_by_the_artifact_limit_before_it_is_buffered():
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal(
        "test", frozenset({"sources:write", "artifacts:write"})
    )
    with TestClient(app) as client:
        refused = client.post(
            "/v1/dropbox", params={"filename": "deck.pptx", "kind": "artifact"}, content=b"x" * 1025
        )
        accepted = client.post(
            "/v1/dropbox", params={"filename": "deck.pptx", "kind": "artifact"}, content=b"x" * 1024
        )
    assert refused.status_code == 413
    assert refused.json()["limit_bytes"] == 1024
    assert accepted.status_code == 202
    assert [action for action, _ in fake.calls] == ["upload-drop"]


def test_review_forwards_the_owner_speaker_choice():
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal(
        "test",
        frozenset({"notes:read", "notes:write", "sources:read", "sources:write", "entities:read"}),
    )
    with TestClient(app) as client:
        result = client.post(
            "/v1/notes/note/review",
            json={
                "if_match": "hash",
                "confirmation": "Complete review",
                "job_id": "job",
                "selected": [2],
                "speaker_choices": {"2": "person:chosen"},
            },
        )
    assert result.status_code == 200
    assert fake.calls[0][1]["speaker_choices"] == {"2": "person:chosen"}


@pytest.mark.parametrize(
    "selected,choices,expected",
    [([0], {}, 403), ([0], {"0": "person:other"}, 403), ([0], {"0": ""}, 200), ([1], {}, 200)],
)
def test_speaker_revision_requires_source_write_but_other_review_remains_available(
    selected, choices, expected
):
    class JobStore(Store):
        async def context_action(self, action, payload):
            if action == "job":
                return {
                    "result": {
                        "suggestions": [
                            {"kind": "speaker", "value": "person:proposed"},
                            {"kind": "field", "value": "work"},
                        ]
                    }
                }
            return await super().context_action(action, payload)

    app = FastAPI()
    fake = JobStore()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal(
        "test", frozenset({"notes:read", "notes:write", "sources:read"})
    )
    with TestClient(app) as client:
        result = client.post(
            "/v1/notes/note/review",
            json={
                "if_match": "hash",
                "confirmation": "Complete review",
                "job_id": "job",
                "selected": selected,
                "speaker_choices": choices,
            },
        )
    assert result.status_code == expected
    assert bool(fake.calls) == (expected == 200)
    if expected == 403:
        assert result.json()["detail"]["error"] == "forbidden"


def test_naming_speakers_requires_sources_write_and_entities_read():
    class ReviewStore(Store):
        async def context_action(self, action, payload):
            if action == "job":
                return {"result": {"suggestions": [{"kind": "speaker", "value": "person:listed"}]}}
            return await super().context_action(action, payload)

    app = FastAPI()
    fake = ReviewStore()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    scopes = {"notes:read", "notes:write", "sources:read"}
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    base = {"if_match": "hash", "confirmation": "Complete review", "job_id": "job"}
    manual = base | {"manual_speakers": {"Speaker 1": "person:listed"}}
    selected = base | {"selected": [0], "speaker_choices": {"0": "person:listed"}}
    with TestClient(app) as client:
        for body in (manual, selected):
            assert client.post("/v1/notes/note/review", json=body).status_code == 403
        scopes.add("sources:write")
        for body in (manual, selected):
            assert client.post("/v1/notes/note/review", json=body).status_code == 403
        scopes.update({"entities:read"})
        for body in (manual, selected):
            assert client.post("/v1/notes/note/review", json=body).status_code == 200
        scopes.discard("sources:write")
        for body in (manual, selected):
            assert client.post("/v1/notes/note/review", json=body).status_code == 403


def test_review_returns_people_only_to_a_key_that_can_read_entities():
    person = {
        "id": "person:a",
        "name": "Alex",
        "email": "alex@example.com",
        "path": "People/Alex.md",
    }

    class ReviewStore(Store):
        async def context_action(self, action, payload):
            if action == "lifecycle":
                return {
                    "note": {"id": "note"},
                    "manual_speaker_review": {"speakers": ["Speaker 1"], "candidates": [person]},
                    "suggestions": [
                        {
                            "index": 0,
                            "kind": "speaker",
                            "value": "person:a",
                            "candidates": [person],
                        },
                        {"index": 1, "kind": "field", "value": "work"},
                    ],
                }
            return await super().context_action(action, payload)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: ReviewStore()
    scopes = {"notes:read", "sources:read"}
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    with TestClient(app) as client:
        narrow = client.get("/v1/notes/note/review")
        scopes.add("entities:read")
        wide = client.get("/v1/notes/note/review")
    assert narrow.status_code == 200
    assert "alex@example.com" not in narrow.text and "People/Alex.md" not in narrow.text
    assert [s["index"] for s in narrow.json()["suggestions"]] == [1]
    assert narrow.json()["manual_speaker_review"] is None
    notice = narrow.json()["speakers_unavailable"]
    assert "cannot read people" in notice and "Connected apps" in notice
    assert "pair this device with a code" in notice
    assert "Read and correct people and companies" in notice
    assert "entities:read" not in notice
    assert wide.json()["people_access_notice"] == notice
    assert wide.json()["manual_speaker_review"]["candidates"] == [person]
    assert [s["index"] for s in wide.json()["suggestions"]] == [0, 1]
    assert not wide.json().get("speakers_unavailable")


# The notice names the ticks Admin shows for a key, so it is checked against that one list.
TICK = {scope: label for _, label, scopes in PERMISSIONS for scope in scopes}


@pytest.mark.parametrize(
    "missing", ["notes:read", "notes:write", "sources:read", "sources:write", "entities:read"]
)
def test_speaker_permission_notice_matches_required_review_scopes(missing):
    label = TICK[missing]
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: Store()
    scopes = {"notes:read", "notes:write", "sources:read", "sources:write", "entities:read"}
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    with TestClient(app) as client:
        notice = client.get("/v1/notes/note/review").json()["people_access_notice"]
        assert label in notice
        scopes.remove(missing)
        result = client.post(
            "/v1/notes/note/review",
            json={
                "if_match": "hash",
                "confirmation": "Complete review",
                "manual_speakers": {"Speaker 1": "person:a"},
            },
        )
        assert result.status_code == 403


@pytest.mark.parametrize(
    "summary,expected",
    [
        ({"summary": "Owner prose", "decisions": "", "action_items": "- Casey: send plan."}, 200),
        ({"summary": 12, "decisions": "", "action_items": ""}, 422),
        ({"summary": "x" * 12001, "decisions": "", "action_items": ""}, 422),
    ],
)
def test_summary_review_accepts_owner_edits_with_note_write_scope(summary, expected):
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal(
        "test", frozenset({"notes:read", "notes:write", "sources:read"})
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/notes/note/review",
            json={
                "if_match": "hash",
                "confirmation": "Complete review",
                "job_id": "job",
                "meeting_summary": summary,
            },
        )
    assert response.status_code == expected
    if expected == 200:
        assert fake.calls[-1][1]["meeting_summary"] == summary
    else:
        assert fake.calls == []


@pytest.mark.parametrize(
    "scopes,expected",
    [
        (["notes:read", "notes:write", "sources:read"], 403),
        (["notes:read", "notes:write", "sources:read", "sources:write"], 200),
    ],
)
def test_recording_merge_requires_source_write(scopes, expected):
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    with TestClient(app) as client:
        result = client.post(
            "/v1/notes/note/recording-merge",
            json={
                "if_match": "hash",
                "target_id": "target",
                "target_hash": "target-hash",
                "job_id": "job",
                "confirmation": "Merge recording",
                "meeting_summary": {"summary": "Summary", "decisions": "", "action_items": ""},
            },
        )
    assert result.status_code == expected
    assert len(fake.calls) == (expected == 200)
