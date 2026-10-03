"""Fields and Tags Admin Page."""

import json
from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from coppermind.schema import FrontmatterSchema
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict, StateStore
from coppermind.store_client import HttpStoreClient

router = APIRouter()


async def get_tag_counts() -> dict[str, int]:
    try:
        settings = Wiring()
        client = HttpStoreClient(settings.store_url, settings.read_internal_token(), timeout=5.0)
        try:
            counts = await client.list_tags()
            return {tc.tag: tc.count for tc in counts}
        finally:
            await client.aclose()
    except Exception:
        return {}


def displayed(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


async def render(
    store: StateStore, message: str = "", status: int = 200, submitted: dict[str, Any] | None = None
) -> HTMLResponse:
    from coppermind_admin import main

    try:
        state = store.read("schema")
    except OSError:
        from coppermind.schema import default_schema

        state = store.ensure("schema", default_schema().model_dump(mode="json"))

    current = FrontmatterSchema.model_validate(
        {key: value for key, value in state.body.items() if key != "revision"}
    )

    revision = submitted.get("revision", "") if submitted else str(state.revision)

    tag_counts = await get_tag_counts()

    body = f'''<h1>Fields & Tags</h1><p><a href="/admin">Overview</a></p>{message}
<p>Revision: {state.revision}</p>
<form method="post" action="/v1/admin/fields">
<input type="hidden" name="revision" value="{escape(revision, quote=True)}">'''

    body += '<textarea name="schema" style="width: 100%; height: 500px; font-family: monospace;">'
    body += escape(current.model_dump_json(indent=2))
    body += "</textarea>"
    body += """
<h2>Tags in Use</h2>
<ul>
"""
    for tag, count in tag_counts.items():
        body += f"<li>{escape(tag)} ({count})</li>"

    body += "</ul><button>Save Schema</button></form>"
    return HTMLResponse(main.page("Fields & Tags", body), status_code=status)


def failure(error: Exception, status: int = 500) -> HTMLResponse:
    from coppermind_admin import main

    return HTMLResponse(
        main.page(
            "Fields & Tags",
            f'<h1>Fields & Tags</h1><p class="error">{escape(main.rejection_detail(error))}</p>'
            '<a href="/admin/fields">Reload fields</a>',
        ),
        status_code=status,
    )


@router.get("/admin/fields", include_in_schema=False)
async def fields_page(request: Request) -> Response:
    try:
        return await render(request.app.state.control)
    except (OSError, ValueError) as exc:
        return failure(exc)


@router.post("/v1/admin/fields", include_in_schema=False)
async def save(request: Request) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    store = request.app.state.control
    try:
        schema_json = body.get("schema", "{}")
        try:
            data = json.loads(schema_json)
        except ValueError as exc:
            raise ValueError("schema: must be valid JSON") from exc

        schema_obj = FrontmatterSchema.model_validate(data)

        try:
            revision = int(body.get("revision", ""))
        except ValueError as exc:
            raise ValueError("revision: a revision is required") from exc

        saved = store.write("schema", schema_obj.model_dump(mode="json"), if_revision=revision)
        return await render(store, f"<p>Saved revision {saved.revision}.</p>")
    except RevisionConflict as exc:
        return failure(exc, 409)
    except ValueError as exc:
        try:
            return await render(
                store, f'<p class="error">{escape(main.rejection_detail(exc))}</p>', 422, dict(body)
            )
        except (OSError, ValueError) as read_error:
            return failure(read_error)
    except OSError as exc:
        return failure(exc)
