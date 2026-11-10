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
from services.admin.tests.test_context_page import ContextStore


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
        self.connections = [{"id": "local", "name": "Lab embeddings", "provider": "compatible"}]
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
                "timeout_s": 60,
            },
            "connections": self.connections,
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
    assert ">Rebuild index</button>" in response.text


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
    assert response.headers["location"] == "/admin/search-index?notice=embeddings-on"
    assert store.saved == {
        "revision": 4,
        "assignment": {
            "connection": "local",
            "model": " nomic-embed ",
            "enabled": True,
            "timeout_s": 60,
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


def test_a_connection_is_added_on_the_connections_page_and_the_way_back_is_offered(signed_in):
    client, _ = signed_in
    store = IndexStore()
    store.connections = []
    client.app.state.store = store

    shown = client.get("/admin/search-index").text

    assert 'href="/admin/context?tab=models&amp;return=/admin/search-index"' in shown or (
        'href="/admin/context?tab=models&return=/admin/search-index"' in shown
    )
    assert "Add or edit connection" in shown
    assert "There is no OpenAI or OpenAI-compatible connection yet." in shown
    assert "where models come from" in shown


def test_a_failed_save_keeps_what_was_typed_and_the_reason_says_what_to_do(signed_in):
    client, _ = signed_in
    store = IndexStore(
        refuse="The service couldn\u2019t find model \u2018wrong-model\u2019. Check the model "
        "name or refresh the model list. Nothing was saved."
    )
    store.connections.append({"id": "other", "name": "Other host", "provider": "compatible"})
    client.app.state.store = store
    token = form_token(client, "/admin/search-index")

    response = client.post(
        "/v1/admin/search-index/embeddings",
        data={
            "csrf": token,
            "revision": "4",
            "connection": "other",
            "model": "wrong-model",
            "timeout_s": "90",
            "enabled": "on",
        },
    )

    assert response.status_code == 422
    assert "Check the model name or refresh the model list. Nothing was saved." in response.text
    assert '<option value="other" selected>Other host</option>' in response.text
    assert 'value="wrong-model"' in response.text
    assert 'name="timeout_s" type="number" min="5" max="600"\nvalue="90"' in response.text
    assert re.search(r'name="enabled" checked', response.text)


def test_the_wait_limit_is_a_setting_here_and_testing_is_announced_on_submit(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()

    shown = client.get("/admin/search-index").text

    assert "Wait limit (seconds)" in shown
    assert 'name="timeout_s"' in shown
    assert "Processing limits" not in shown
    assert "Testing connection... this may take up to" in shown


def test_an_out_of_range_wait_limit_is_refused_before_the_store(signed_in):
    client, _ = signed_in
    store = IndexStore()
    client.app.state.store = store
    token = form_token(client, "/admin/search-index")

    response = client.post(
        "/v1/admin/search-index/embeddings",
        data={"csrf": token, "revision": "4", "connection": "local", "timeout_s": "9000"},
    )

    assert response.status_code == 422
    assert "between 5 and 600 seconds. Nothing was saved." in response.text
    assert store.saved == {}


def test_saving_with_search_off_says_so_and_never_says_indexing_started(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()

    shown = client.get("/admin/search-index?notice=embeddings-off").text

    assert "Search by meaning is off. Searches use keywords only." in shown
    assert "Indexing by meaning has started" not in shown
    assert "Embedding runs in the background" not in shown


def test_saving_on_says_how_far_indexing_is(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(
        embeddings=EmbeddingStatus(state="working", model="m", documents=340, embedded=119)
    )

    shown = client.get("/admin/search-index?notice=embeddings-on").text

    assert "Saved. Indexing by meaning has started; 119 of 340 entries are ready." in shown


def test_changing_connection_or_model_is_explained_and_refresh_is_manual(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(
        embeddings=EmbeddingStatus(state="working", model="m", documents=10, embedded=3)
    )

    shown = " ".join(client.get("/admin/search-index").text.split())

    assert (
        "Changing the connection or the model indexes existing content again; keyword search "
        "remains available while it does." in shown
    )
    assert "does not refresh by itself" in shown
    assert "Refresh progress" in shown


def test_connections_opened_from_search_offer_the_way_back_and_return_after_saving(signed_in):
    client, _ = signed_in
    store = ContextStore()
    client.app.state.store = store

    opened = client.get("/admin/context?tab=models&return=/admin/search-index")
    assert 'href="/admin/search-index">Back to Search index' in opened.text
    assert 'name="return" value="/admin/search-index"' in opened.text

    plain = client.get("/admin/context?tab=models").text
    assert "Back to Search index" not in plain
    assert 'name="return"' not in plain

    token = form_token(client, "/admin/context?tab=models")
    saved = client.post(
        "/v1/admin/context/save-connection",
        data={
            "csrf": token,
            "revision": "0",
            "return": "/admin/search-index",
            "id": "",
            "name": "New",
            "provider": "compatible",
            "base_url": "http://embed.example/v1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/admin/search-index?notice=connection-saved"

    elsewhere = client.post(
        "/v1/admin/context/save-connection",
        data={
            "csrf": token,
            "revision": "0",
            "return": "https://evil.example/",
            "id": "",
            "name": "New",
            "provider": "compatible",
            "base_url": "http://embed.example/v1",
        },
        follow_redirects=False,
    )
    assert elsewhere.headers["location"].startswith("/admin/context?tab=models")


def test_a_stale_save_says_what_happened_keeps_the_draft_and_carries_the_current_revision(
    signed_in,
):
    client, _ = signed_in
    store = IndexStore(refuse="configuration changed; reload before saving")
    client.app.state.store = store
    token = form_token(client, "/admin/search-index")

    response = client.post(
        "/v1/admin/search-index/embeddings",
        data={
            "csrf": token,
            "revision": "2",
            "connection": "local",
            "model": "my-draft-model",
            "timeout_s": "75",
            "enabled": "on",
        },
    )

    assert response.status_code == 422
    assert (
        "Settings changed in another session. Nothing was saved. Your entries are still here; "
        "review them and save again." in response.text
    )
    assert "reload" not in response.text.lower().split("<main")[1]
    assert 'value="my-draft-model"' in response.text
    assert 'value="75"' in response.text
    # The stand-in's current revision is 4; saving again must use it, not the stale 2.
    assert 'name="revision" value="4"' in response.text


def test_other_refusals_keep_the_stores_own_words(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore(refuse="Some other refusal. Nothing was saved.")
    token = form_token(client, "/admin/search-index")

    response = client.post(
        "/v1/admin/search-index/embeddings",
        data={"csrf": token, "revision": "4", "connection": "local", "model": "m"},
    )

    assert "Some other refusal. Nothing was saved." in response.text
    assert "Settings changed in another session" not in response.text


def test_a_refused_wait_limit_stays_as_typed_and_is_marked_invalid(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()
    token = form_token(client, "/admin/search-index")

    response = client.post(
        "/v1/admin/search-index/embeddings",
        data={"csrf": token, "revision": "4", "connection": "local", "timeout_s": "9000"},
    )

    assert response.status_code == 422
    assert 'value="9000" aria-invalid="true"' in response.text
    assert 'value="60"' not in response.text.split('name="timeout_s"')[1].split(">")[0]

    garbled = client.post(
        "/v1/admin/search-index/embeddings",
        data={"csrf": token, "revision": "4", "connection": "local", "timeout_s": "soon"},
    )
    assert 'value="soon" aria-invalid="true"' in garbled.text

    fine = client.get("/admin/search-index").text
    assert 'aria-invalid="true"' not in fine


def test_leaving_for_connections_warns_that_unsaved_settings_are_lost(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()

    shown = " ".join(client.get("/admin/search-index").text.split())

    assert (
        "unsaved changes to the settings below are lost when you leave, so save them first" in shown
    )
    assert 'id="connections-link"' in shown
    assert "Leave anyway?" in shown
