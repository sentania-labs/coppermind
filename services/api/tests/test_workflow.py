"""The companion cannot use note-only credentials to read source evidence."""

import pytest
from coppermind_api.auth import Principal
from coppermind_api.deps import authenticated_key, store
from coppermind_api.v1.workflow import router
from fastapi import FastAPI
from fastapi.testclient import TestClient


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


def test_manual_speaker_and_unlisted_person_require_their_respective_scopes():
    class ReviewStore(Store):
        async def context_action(self, action, payload):
            if action == "lifecycle":
                return {"manual_speaker_review": {"candidates": [{"id": "person:listed"}]}}
            if action == "job":
                return {
                    "result": {"suggestions": [{"kind": "speaker", "value": "person:listed"}]},
                    "evidence": {"speaker_review": {"candidates": [{"id": "person:listed"}]}},
                }
            return await super().context_action(action, payload)

    app = FastAPI()
    fake = ReviewStore()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    scopes = {"notes:read", "notes:write", "sources:read"}
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    base = {"if_match": "hash", "confirmation": "Complete review", "job_id": "job"}
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/notes/note/review",
                json=base | {"manual_speakers": {"Speaker 1": "person:listed"}},
            ).status_code
            == 403
        )
        scopes.add("sources:write")
        assert (
            client.post(
                "/v1/notes/note/review",
                json=base | {"manual_speakers": {"Speaker 1": "person:unlisted"}},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/notes/note/review",
                json=base | {"manual_speakers": {"Speaker 1": "person:listed"}},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/notes/note/review",
                json=base | {"selected": [0], "speaker_choices": {"0": "person:unlisted"}},
            ).status_code
            == 403
        )
        scopes.add("entities:read")
        assert (
            client.post(
                "/v1/notes/note/review",
                json=base | {"selected": [0], "speaker_choices": {"0": "person:unlisted"}},
            ).status_code
            == 200
        )
