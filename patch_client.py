import re

with open("coppermind/store_client.py", "r") as f:
    content = f.read()

new_imports = "SchemaResponse, TagCount,"
content = content.replace("ReplaceNote,", "ReplaceNote, SchemaResponse, TagCount,")

new_methods = """
    async def get_schema(self) -> SchemaResponse:
        response = await self._send("GET", f"{INTERNAL_PREFIX}/schema")
        return SchemaResponse.model_validate(response.json())

    async def put_schema(self, schema_doc: dict[str, Any], if_revision: int) -> SchemaResponse:
        response = await self._send(
            "PUT",
            f"{INTERNAL_PREFIX}/schema",
            json=schema_doc,
            headers={"If-Match": str(if_revision)},
        )
        return SchemaResponse.model_validate(response.json())
"""

content = content.replace("async def get_api_keys(self) -> ApiKeySet:", new_methods.strip() + "\n\n    async def get_api_keys(self) -> ApiKeySet:")

content = content.replace("from typing import annotations", "from typing import annotations, Any")
if "from typing import Any" not in content:
    content = content.replace("from __future__ import annotations\n", "from __future__ import annotations\n\nfrom typing import Any\n")

with open("coppermind/store_client.py", "w") as f:
    f.write(content)
