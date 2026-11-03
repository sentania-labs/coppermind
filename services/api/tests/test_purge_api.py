"""The new purge privilege is opt-in, including for existing admin keys."""

from datetime import UTC, datetime

from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import API_SCOPES, ApiKeySet, create_key
from coppermind.purge import PurgeCounts, PurgeResult
from coppermind.settings import Wiring


class PurgeStore:
    def __init__(self):
        self.old, self.old_key = create_key(
            "Existing admin", [s for s in API_SCOPES if s != "admin:purge"]
        )
        self.operator, self.operator_key = create_key("Purge operator", ["admin:purge"])
        self.confirmed = []
        self.abandoned = []

    async def get_api_keys(self):
        return ApiKeySet(keys=[self.old, self.operator])

    async def preview_purge(self):
        return PurgeResult(
            token="a" * 64,
            state="preview",
            created_at=datetime.now(UTC),
            retention_days=30,
            candidates=[],
            counts=PurgeCounts(),
        )

    async def confirm_purge(self, payload):
        self.confirmed.append(payload)
        return (await self.preview_purge()).model_copy(update={"state": "complete"})

    async def abandon_purge(self, payload):
        self.abandoned.append(payload)
        return (await self.preview_purge()).model_copy(update={"state": "abandoned"})


def test_scope_and_explicit_confirmation(tmp_path):
    token = tmp_path / "internal"
    token.write_text("internal-test")
    app = create_app(Wiring(internal_token_file=token))
    fake = PurgeStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.api_key_auth._store = fake
        for route in ["preview", "confirm", "abandon"]:
            body = (
                {"token": "a" * 64, "confirmation": "Purge listed items"}
                if route == "confirm"
                else {"token": "a" * 64}
                if route == "abandon"
                else {}
            )
            url = "/v1/admin/purge/" + route
            assert client.post(url, json=body).status_code == 401
            assert (
                client.post(
                    url, json=body, headers={"Authorization": f"Bearer {fake.old_key}"}
                ).status_code
                == 403
            )
            assert (
                client.post(
                    url, json=body, headers={"Authorization": f"Bearer {fake.operator_key}"}
                ).status_code
                == 200
            )
        assert len(fake.confirmed) == 1 and len(fake.abandoned) == 1
        assert (
            client.post(
                "/v1/admin/purge/confirm",
                json={"token": "a" * 64},
                headers={"Authorization": f"Bearer {fake.operator_key}"},
            ).status_code
            == 422
        )
        for verb in ["put", "patch", "delete"]:
            assert (
                getattr(client, verb)(
                    "/v1/sources/anything", headers={"Authorization": f"Bearer {fake.operator_key}"}
                ).status_code
                == 405
            )
