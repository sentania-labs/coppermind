import re

with open("services/admin/coppermind_admin/main.py", "r") as f:
    content = f.read()

content = content.replace("from coppermind.settings import ProductSettings, Wiring, read_settings", "from coppermind.settings import ProductSettings, Wiring, read_settings\nfrom coppermind.store_client import HttpStoreClient")

new_store = """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = HttpStoreClient(
            settings.store_url,
            settings.read_internal_token(),
            timeout=settings.store_timeout_s,
        )
        app.state.store = client
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Coppermind Admin", version=version, lifespan=lifespan)
"""

content = content.replace("    app = FastAPI(title=\"Coppermind Admin\", version=version)", new_store)
content = content.replace("from contextvars import ContextVar", "from contextlib import asynccontextmanager\nfrom contextvars import ContextVar")

with open("services/admin/coppermind_admin/main.py", "w") as f:
    f.write(content)
