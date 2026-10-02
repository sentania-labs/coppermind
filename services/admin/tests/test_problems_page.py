from services.admin.tests.browser import admin_browser as admin_browser


def test_problems_page(signed_in):
    client, wiring = signed_in

    response = client.get("/admin/problems")
    assert response.status_code == 200
    assert "Problems" in response.text


def test_overview_status(signed_in):
    client, wiring = signed_in

    class MockStore:
        async def get_status(self):
            from coppermind.store_protocol import StatusCounters, StatusResponse

            return StatusResponse(
                counters=StatusCounters(
                    notes_awaiting_review=5,
                    sources=10,
                    notes_by_state={},
                    rejected_ingests=0,
                    name_collisions=0,
                    unparseable_files=0,
                )
            )

    client.app.state.store = MockStore()

    response = client.get("/admin")
    assert response.status_code == 200
    assert "Store Status" in response.text
    assert "Notes awaiting review: 5" in response.text
