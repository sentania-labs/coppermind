from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/schema", tags=["schema"])

@router.get("")
async def get_schema(
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:read")),
) -> JSONResponse:
    try:
        schema_resp = await client.get_schema()
    except StoreError as error:
        return failure(error)
        
    doc = schema_resp.schema_doc
    # Map fields
    fields = []
    for k in doc.get("keys", []):
        fields.append({
            "name": k.get("name"),
            "kind": k.get("kind"),
            "required": k.get("required"),
            "required_when": k.get("required_when"),
            "required_when_values": k.get("required_when_values"),
            "guidance": k.get("guidance"),
            "allowed_values": k.get("vocabulary")
        })
        
    tags_data = doc.get("tags", {})
        
    res = {
        "fields": fields,
        "allowed_values": {k.get("name"): k.get("vocabulary") for k in doc.get("keys", []) if k.get("vocabulary")},
        "tags": [{"tag": t.tag, "count": t.count} for t in schema_resp.tag_counts],
        "aliases": tags_data.get("aliases", {}),
        "guidance": {k.get("name"): k.get("guidance") for k in doc.get("keys", []) if k.get("guidance")},
        "open_tags": tags_data.get("open", True)
    }
    
    return JSONResponse(status_code=200, content=res)
