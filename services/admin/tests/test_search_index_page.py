"""The search index page: its state in local time and the rebuild button.

Admin reaches the store over the internal contract, so a stand-in answers the
two calls this page makes and fails the test on any other.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from coppermind.store_protocol import (
    EmbeddingStatus,
    RebuildSearchIndexResult,
    SearchIndexStatus,
    StoreUnavailable,
    ValidationFailed,
)
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class IndexStore:
    def __init__(
        self,
        *,
        reachable: bool = True,
        running: bool = False,
        embeddings: EmbeddingStatus | None = None,
        refuse: str = "",
    ) -> None:
        self.embeddings = embeddings or EmbeddingStatus()
        self.refuse = refuse
        self.saved: dict = {}
        self.reachable = reachable
        self.running = running
        self.calls: list[str] = []

    def _status(self) -> SearchIndexStatus:
        return SearchIndexStatus(
            notes=42,
            sources=3,
            last_updated_at=datetime(2026, 10, 9, 19, 5, tzinfo=UTC),
            rebuild_running=self.running,
            rebuild_started_at=datetime(2026, 10, 9, 18, 0, tzinfo=UTC),
            rebuild_completed_at=datetime(2026, 10, 9, 18, 1, tzinfo=UTC),
            embeddings=self.embeddings,
        )

    async def get_search_index_status(self) -> SearchIndexStatus:
        self.calls.append("get_search_index_status")
        if not self.reachable:
            raise StoreUnavailable("the store is down")
        return self._status()

    async def rebuild_search_index(self) -> RebuildSearchIndexResult:
        self.calls.append("rebuild_search_index")
        if not self.reachable:
            raise StoreUnavailable("the store is down")
        started = not self.running
        self.running = True
        return RebuildSearchIndexResult(started=started, status=self._status())

    async def context_action(self, action: str, payload: dict) -> dict:
        self.calls.append(action)
        if not self.reachable:
            raise StoreUnavailable("the store is down")
        if action == "save-embeddings":
            self.saved = payload
            if self.refuse:
                raise ValidationFailed([self.refuse])
            return {"saved": "embeddings"}
        return {
            "revision": 4,
            "embeddings": {
                "connection": "local",
                "model": "nomic-embed",
                "enabled": True,
            },
            "connections": [{"id": "local", "name": "Lab embeddings", "provider": "compatible"}],
            "discovery": {"local": ["nomic-embed", "bge-small"]},
        }

    async def get_status(self) -> None:
        """The overview's counters, which this stand-in has none of."""
        raise StoreUnavailable("not this page's concern")

    def __getattr__(self, name: str):
        raise AssertionError(f"Admin asked the store for {name}, which this page never needs")


def test_settings_links_to_the_search_index(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()

    assert '<a href="/admin/search-index">Search index</a>' in client.get("/admin/settings").text


def test_the_page_shows_counts_and_times_in_local_time(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()

    response = client.get("/admin/search-index")

    assert response.status_code == 200
    assert "Notes indexed: 42" in response.text
    assert "Source pages indexed: 3" in response.text
    assert "Last updated: October 9, 2026, 2:05 PM CDT" in response.text
    assert "last finished October 9, 2026, 1:01 PM CDT" in response.text
    assert "<button>Rebuild index</button>" in response.text


def test_rebuild_index_starts_a_rebuild_and_says_so(signed_in):
    client, _ = signed_in
    store = IndexStore()
    client.app.state.store = store
    token = form_token(client, "/admin/search-index")

    first = client.post(
        "/v1/admin/search-index/rebuild", data={"csrf": token}, follow_redirects=False
    )
    second = client.post(
        "/v1/admin/search-index/rebuild", data={"csrf": token}, follow_redirects=False
    )

    assert first.status_code == 303
    assert first.headers["location"] == "/admin/search-index?notice=started"
    assert second.headers["location"] == "/admin/search-index?notice=running"
    shown = client.get("/admin/search-index?notice=started").text
    assert "Rebuild started." in shown
    assert re.search(r"Rebuild: running since October 9, 2026, 1:00 PM CDT", shown)
    assert store.calls.count("rebuild_search_index") == 2


def test_rebuild_needs_the_form_token(signed_in):
    client, _ = signed_in
    store = IndexStore()
    client.app.state.store = store

    response = client.post("/v1/admin/search-index/rebuild", data={})

    assert response.status_code == 403
    assert "rebuild_search_index" not in store.calls


def test_an_unreachable_store_is_named_and_nothing_is_rebuilt(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(reachable=False)
    page = client.get("/admin/search-index")
    token = form_token(client, "/admin/search-index")

    refused = client.post("/v1/admin/search-index/rebuild", data={"csrf": token})

    assert page.status_code == 503
    assert "The store could not answer" in page.text
    assert refused.status_code == 503
    assert "Nothing was rebuilt." in refused.text


def test_the_page_needs_a_session(tmp_path):
    from services.admin.tests.test_admin import _client, claim

    client, _, _ = _client(tmp_path)
    with client:
        claim(client)
        client.app.state.store = IndexStore()
        response = client.get("/admin/search-index", follow_redirects=False)
        assert response.status_code == 303


def test_progress_is_shown_in_words_and_a_bar(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(
        embeddings=EmbeddingStatus(
            state="working", model="nomic-embed", documents=340, embedded=119, passages=500
        )
    )

    shown = client.get("/admin/search-index").text

    assert "Embedding your notes with nomic-embed: 119 of 340 done (35%)" in shown
    assert '<progress max="340" value="119">' in shown
    assert "Searches use keywords, plus meaning for what is done." in shown


def test_a_model_that_is_not_answering_says_so_and_what_search_does(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(
        embeddings=EmbeddingStatus(
            state="waiting",
            model="nomic-embed",
            documents=10,
            embedded=2,
            error="The model provider accepted the request but sent nothing for 60 s.",
            retry_at=datetime(2026, 10, 9, 19, 30, tzinfo=UTC),
        )
    )

    shown = client.get("/admin/search-index").text

    assert "The embeddings model is not answering: The model provider accepted" in shown
    assert "Next try October 9, 2026, 2:30 PM CDT." in shown
    assert "Searches use keywords only until it does." in shown


def test_when_everything_is_embedded_it_says_so(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(
        embeddings=EmbeddingStatus(
            state="idle", model="nomic-embed", documents=340, embedded=340, passages=900
        )
    )

    assert "All 340 entries are ready for meaning search (900 passages" in (
        client.get("/admin/search-index").text
    )


def test_the_settings_form_offers_discovered_models_and_names_where_text_goes(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()

    shown = client.get("/admin/search-index").text

    assert '<option value="local" selected>Lab embeddings</option>' in shown
    assert '<option value="bge-small">' in shown
    assert 'value="nomic-embed"' in shown
    assert "is\nsent to the connection chosen above" in shown


def test_saving_sends_the_choice_and_returns_to_the_page(signed_in):
    client, _ = signed_in
    store = IndexStore()
    client.app.state.store = store
    token = form_token(client, "/admin/search-index")

    response = client.post(
        "/v1/admin/search-index/embeddings",
        data={
            "csrf": token,
            "revision": "4",
            "connection": "local",
            "model": " nomic-embed ",
            "enabled": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/search-index?notice=embeddings-saved"
    assert store.saved == {
        "revision": 4,
        "assignment": {
            "connection": "local",
            "model": " nomic-embed ",
            "enabled": True,
        },
    }


def test_a_refused_save_shows_the_reason_beside_the_form(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(refuse="The embeddings model did not answer a test.")
    token = form_token(client, "/admin/search-index")

    response = client.post(
        "/v1/admin/search-index/embeddings",
        data={"csrf": token, "revision": "4", "connection": "local", "model": "x"},
    )

    assert response.status_code == 422
    assert "The embeddings model did not answer a test." in response.text
    assert "Save search by meaning" in response.text


def test_saving_needs_the_form_token(signed_in):
    client, _ = signed_in
    store = IndexStore()
    client.app.state.store = store

    assert client.post("/v1/admin/search-index/embeddings", data={}).status_code == 403
    assert store.saved == {}
