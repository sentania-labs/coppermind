"""Artifact hits carry extracted text, so only a key that may read artifacts sees them."""

import pytest
from coppermind_api.auth import Principal
from coppermind_api.deps import authenticated_key, store
from coppermind_api.v1.search import router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coppermind.store_protocol import Page, SearchHit, SearchQuery


class Store:
    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []

    async def search(
        self, query: SearchQuery, *, entities: bool = False, sources: bool = True
    ) -> Page[SearchHit]:
        self.queries.append(query)
        return Page[SearchHit](items=[], next_cursor=None)


@pytest.mark.parametrize(
    "scopes,params,artifacts_included",
    [
        (["notes:read"], {}, False),
        (["notes:read"], {"include_artifacts": "true"}, False),
        (["notes:read"], {"kind": "artifact"}, False),
        (["notes:read", "artifacts:read"], {}, True),
    ],
)
def test_search_leaves_artifact_hits_out_for_a_key_without_artifact_read(
    scopes, params, artifacts_included
):
    app = FastAPI()
    fake = Store()
    app.include_router(router)
    app.dependency_overrides[store] = lambda: fake
    app.dependency_overrides[authenticated_key] = lambda: Principal("test", frozenset(scopes))
    with TestClient(app) as client:
        result = client.get("/v1/search", params={"q": "deck", **params})
    assert result.status_code == 200
    assert fake.queries[0].include_artifacts is artifacts_included
