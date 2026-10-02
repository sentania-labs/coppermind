import json
from html import escape
from typing import Any

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, Response

from coppermind.schema import FrontmatterSchema, KeyDefinition
from coppermind.statefiles import RevisionConflict, StateStore

router = APIRouter()

def render(
    store: StateStore, request: Request, message: str = "", status: int = 200, submitted: dict[str, Any] | None = None, tag_counts: list[Any] | None = None
) -> HTMLResponse:
    from coppermind_admin import main

    state = store.read("schema")
    current = FrontmatterSchema.model_validate(
        {key: value for key, value in state.body.items() if key != "revision"}
    )
    revision = submitted.get("revision", "") if submitted else str(state.revision)
    
    if tag_counts is None:
        tag_counts = []
        
    counts_map = {t.tag: t.count for t in tag_counts}

    body = f'''<h1>Fields and Tags</h1><p><a href="/admin">Overview</a></p>{message}
<p>Revision: {state.revision}</p>
<form method="post" action="/v1/admin/schema">
<input type="hidden" name="revision" value="{escape(revision, quote=True)}">
'''

    # Render fields
    body += "<h2>Fields</h2>"
    for i, field in enumerate(current.keys):
        prefix = f"keys.{i}"
        
        name_val = submitted.get(f"{prefix}.name", field.name) if submitted else field.name
        kind_val = submitted.get(f"{prefix}.kind", field.kind) if submitted else field.kind
        req_val = submitted.get(f"{prefix}.required", field.required) if submitted else field.required
        guidance_val = submitted.get(f"{prefix}.guidance", field.guidance) if submitted else field.guidance
        req_when_val = submitted.get(f"{prefix}.required_when", field.required_when or "") if submitted else (field.required_when or "")
        req_when_vals_val = submitted.get(f"{prefix}.required_when_values", json.dumps(field.required_when_values)) if submitted else json.dumps(field.required_when_values)
        vocab_val = submitted.get(f"{prefix}.vocabulary", json.dumps(field.vocabulary)) if submitted else json.dumps(field.vocabulary)
        
        checked = "checked" if str(req_val).lower() == "true" or req_val is True else ""

        body += f'''<fieldset><legend>{escape(name_val)}</legend>
<label>Name<input name="{prefix}.name" value="{escape(name_val, quote=True)}"></label>
<label>Kind<input name="{prefix}.kind" value="{escape(kind_val, quote=True)}"></label>
<label><input type="checkbox" name="{prefix}.required" value="true" {checked}> Required</label>
<label>Required When<input name="{prefix}.required_when" value="{escape(req_when_val, quote=True)}"></label>
<label>Required When Values (JSON list)<input name="{prefix}.required_when_values" value="{escape(req_when_vals_val, quote=True)}"></label>
<label>Guidance<input name="{prefix}.guidance" value="{escape(guidance_val, quote=True)}"></label>
<label>Allowed Values (JSON dict: value -> meaning)<input name="{prefix}.vocabulary" value="{escape(vocab_val, quote=True)}"></label>
</fieldset>'''

    # Render tag settings
    body += "<h2>Tags</h2>"
    open_val = submitted.get("tags.open", current.tags.open) if submitted else current.tags.open
    meanings_val = submitted.get("tags.meanings", json.dumps(current.tags.meanings)) if submitted else json.dumps(current.tags.meanings)
    aliases_val = submitted.get("tags.aliases", json.dumps(current.tags.aliases)) if submitted else json.dumps(current.tags.aliases)
    
    open_checked = "checked" if str(open_val).lower() == "true" or open_val is True else ""

    body += f'''<fieldset><legend>Tag Settings</legend>
<label><input type="checkbox" name="tags.open" value="true" {open_checked}> Open Tags (Accept unknown tags)</label>
<label>Meanings (JSON dict: tag -> meaning)<input name="tags.meanings" value="{escape(meanings_val, quote=True)}"></label>
<label>Aliases (JSON dict: alias -> canonical)<input name="tags.aliases" value="{escape(aliases_val, quote=True)}"></label>
</fieldset>'''

    body += "<h3>Tag Counts (from mirror)</h3><ul>"
    for tag, count in sorted(counts_map.items()):
        body += f"<li>{escape(tag)}: {count}</li>"
    if not counts_map:
        body += "<li>None</li>"
    body += "</ul>"

    body += "<button>Save schema</button></form>"
    return HTMLResponse(main.page("Fields and Tags", body), status_code=status)


def failure(error: Exception, status: int = 500) -> HTMLResponse:
    from coppermind_admin import main

    return HTMLResponse(
        main.page(
            "Fields and Tags",
            f'<h1>Fields and Tags</h1><p class="error">{escape(main.rejection_detail(error))}</p>'
            '<a href="/admin/fields">Reload fields</a>',
        ),
        status_code=status,
    )


@router.get("/admin/fields", include_in_schema=False)
async def fields_page(request: Request) -> Response:
    try:
        tag_counts = await request.app.state.store.get_tag_counts()
        return render(request.app.state.control, request, tag_counts=tag_counts)
    except (OSError, ValueError) as exc:
        return failure(exc)


@router.post("/v1/admin/schema", include_in_schema=False)
async def save(request: Request) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    store = request.app.state.control
    tag_counts = []
    try:
        tag_counts = await request.app.state.store.get_tag_counts()
    except Exception:
        pass

    try:
        current_state = store.read("schema")
        current_schema = FrontmatterSchema.model_validate(
            {key: value for key, value in current_state.body.items() if key != "revision"}
        )
        
        # We need to construct the new schema from form data
        data: dict[str, Any] = {"schema_version": current_schema.schema_version, "roles": current_schema.roles}
        
        keys = []
        # parse keys
        i = 0
        while f"keys.{i}.name" in body:
            prefix = f"keys.{i}"
            key_data = {
                "name": body[f"{prefix}.name"],
                "kind": body[f"{prefix}.kind"],
                "required": body.get(f"{prefix}.required") == "true",
                "guidance": body.get(f"{prefix}.guidance", ""),
                "required_when": body.get(f"{prefix}.required_when", "") or None,
            }
            try:
                key_data["required_when_values"] = json.loads(body.get(f"{prefix}.required_when_values", "[]"))
            except ValueError:
                raise ValueError(f"{prefix}.required_when_values: must be JSON list")
            try:
                key_data["vocabulary"] = json.loads(body.get(f"{prefix}.vocabulary", "{}"))
            except ValueError:
                raise ValueError(f"{prefix}.vocabulary: must be JSON dict")
            
            keys.append(key_data)
            i += 1
            
        data["keys"] = keys
        
        tags_data = {
            "open": body.get("tags.open") == "true",
        }
        try:
            tags_data["meanings"] = json.loads(body.get("tags.meanings", "{}"))
        except ValueError:
            raise ValueError("tags.meanings: must be JSON dict")
        try:
            tags_data["aliases"] = json.loads(body.get("tags.aliases", "{}"))
        except ValueError:
            raise ValueError("tags.aliases: must be JSON dict")
            
        data["tags"] = tags_data
        
        new_schema = FrontmatterSchema.model_validate(data)
        
        try:
            revision = int(body.get("revision", ""))
        except ValueError as exc:
            raise ValueError("revision: a revision is required") from exc
            
        saved = store.write("schema", new_schema.model_dump(mode="json"), if_revision=revision)
        return render(store, request, f"<p>Saved revision {saved.revision}.</p>", tag_counts=tag_counts)
    except RevisionConflict as exc:
        return failure(exc, 409)
    except ValueError as exc:
        try:
            return render(
                store, request, f'<p class="error">{escape(main.rejection_detail(exc))}</p>', 422, body, tag_counts=tag_counts
            )
        except (OSError, ValueError) as read_error:
            return failure(read_error)
    except OSError as exc:
        return failure(exc)
