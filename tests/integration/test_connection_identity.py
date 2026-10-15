"""Automatic references and atomic conversion preserve existing enrichment state."""

import pytest
from coppermind_store.context_service import fingerprint

from coppermind.context_config import ContextConfig, FeatureAssignment, ModelConnection


async def test_new_ids_are_generated_and_stable(store):
    context = store.context
    fields = dict(name="My server", provider="compatible", base_url="http://local.example/v1")
    saved = await context.save_connection({"revision": 0, "connection": fields})
    identity = saved["saved"]
    assert identity.startswith("conn_")
    assert identity in context.config().automatic_connection_ids
    saved = await context.save_connection(
        {"revision": 1, "connection": dict(fields, id=identity, name="Renamed")}
    )
    assert saved["saved"] == identity
    with pytest.raises(ValueError, match="does not exist"):
        await context.save_connection({"revision": 2, "connection": dict(fields, id="invented")})
    with pytest.raises(ValueError, match="refresh models"):
        await context.test_model({"connection": identity, "model": "invented-model"})


async def test_conversion_preserves_credentials_tests_assignments_and_job_fingerprint(
    store, monkeypatch
):
    context = store.context
    connection = ModelConnection(id="my-made-up-id", name="Existing", provider="openai")
    config = ContextConfig(
        connections=[connection],
        assignments={
            "enrichment": FeatureAssignment(
                connection=connection.id, model="test-model", enabled=True
            )
        },
    )
    context.save_config(config)
    context.write("credentials", {connection.id: "synthetic-test-credential"})
    context.write("discovery", {connection.id: ["test-model"]})
    token = fingerprint({"connection": connection.model_dump(), "model": "test-model"})
    context.write("model_tests", {token: "passed"})
    original_hash = context.config_hash()
    original_config = context.config().model_dump()
    save = context.save_config

    def fail(config):
        raise OSError("simulated write failure")

    monkeypatch.setattr(context, "save_config", fail)
    with pytest.raises(OSError):
        await context.convert_connection({"revision": 1, "connection": connection.id})
    assert context.config().model_dump() == original_config
    monkeypatch.setattr(context, "save_config", save)
    result = await context.convert_connection({"revision": 1, "connection": connection.id})
    identity = result["saved"]
    assert identity != connection.id
    assert context.config().assignments["enrichment"].connection == identity
    assert context.config().assignments["enrichment"].enabled
    assert context.key(identity) == "synthetic-test-credential"
    assert context.config_hash() == original_hash
    snapshot = await context.snapshot()
    assert snapshot["discovery"][identity] == ["test-model"]
    assert "synthetic-test-credential" not in str(snapshot)
    await context.save_assignment(
        {"revision": 2, "assignment": context.config().assignments["enrichment"].model_dump()}
    )
    assert context.config().assignments["enrichment"].enabled
    with pytest.raises(ValueError, match="configuration changed"):
        await context.convert_connection({"revision": 1, "connection": connection.id})
    assert (await context.convert_connection({"revision": 3, "connection": identity}))[
        "saved"
    ] == identity


async def test_unknown_conversion_returns_validation_error(store):
    import httpx
    from coppermind_store.auth import InternalAuth
    from coppermind_store.context_api import router
    from fastapi import FastAPI

    app = FastAPI()
    app.state.store = store
    app.state.auth = InternalAuth("synthetic-internal-token")
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://store"
    ) as client:
        response = await client.post(
            "/internal/v1/context/convert-connection",
            headers={"Authorization": "Bearer synthetic-internal-token"},
            json={"revision": 0, "connection": "missing"},
        )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


@pytest.mark.parametrize(
    "target",
    ["fields", "attendees_field", "invitees_field", "event_field", "contacts_field"],
)
async def test_renamed_schema_version_is_not_an_enrichment_target(store, target):
    schema = store.control.schema()
    original = schema.role("schema_version_key")
    for definition in schema.keys:
        if definition.name == original:
            definition.name = "format_version"
    schema.roles["schema_version_key"] = "format_version"
    state = store.control.store.read("schema")
    store.control.store.write("schema", schema.model_dump(mode="json"), if_revision=state.revision)

    snapshot = await store.context.snapshot()
    names = {field["name"] for field in snapshot["enrichment_fields"]}
    assert "format_version" not in names
    assert names  # Ordinary metadata remains selectable.
    assignment = FeatureAssignment().model_dump()
    assignment[target] = ["format_version"] if target == "fields" else "format_version"
    with pytest.raises(ValueError, match="cannot be system fields"):
        await store.context.save_assignment({"revision": 0, "assignment": assignment})
    assert store.context.config().revision == 0
