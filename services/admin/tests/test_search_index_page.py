"""The search index page: its state in local time and the rebuild button.

Admin reaches the store over the internal contract, so a stand-in answers the
two calls this page makes and fails the test on any other.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from coppermind.store_protocol import (
    RebuildSearchIndexResult,
    SearchIndexStatus,
    StoreUnavailable,
)
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class IndexStore:
    def __init__(self, *, reachable: bool = True, running: bool = False) -> None:
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

    async def get_status(self) -> None:
        """The overview's counters, which this stand-in has none of."""
        raise StoreUnavailable("not this page's concern")

    def __getattr__(self, name: str):
        raise AssertionError(f"Admin asked the store for {name}, which this page never needs")


def test_the_overview_links_to_the_search_index(signed_in):
    client, _ = signed_in
    client.app.state.store = IndexStore()

    assert '<a href="/admin/search-index">Search index</a>' in client.get("/admin").text


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
    token = form_token(client, "/admin")

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
