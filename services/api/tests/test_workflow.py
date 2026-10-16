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
