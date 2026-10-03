from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from coppermind.statefiles import StateStore
from coppermind_api.deps import require_scopes

router = APIRouter(prefix="/v1/schema", tags=["schema"])


@router.get("")
def get_schema(request: Request, scope: None = Depends(require_scopes("notes:read"))):
    settings = request.app.state.wiring
    store = StateStore(settings.state_dir)
    try:
        state = store.read("schema")
        return JSONResponse(content=state.body)
    except OSError:
        from coppermind.schema import default_schema

        schema = default_schema()
        return JSONResponse(content=schema.model_dump(mode="json"))
