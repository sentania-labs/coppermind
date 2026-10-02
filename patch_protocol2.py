import re

with open("coppermind/store_protocol.py", "r") as f:
    content = f.read()

new_classes = """

class SchemaResponse(BaseModel):
    revision: int
    schema_doc: dict[str, Any]
    tag_counts: list[TagCount]
"""
content = content.replace("class Page[T](BaseModel):", new_classes + "\nclass Page[T](BaseModel):")

new_methods = """
    async def get_schema(self) -> SchemaResponse: ...
"""
content = content.replace("async def get_tag_counts(self) -> list[TagCount]: ...", "async def get_tag_counts(self) -> list[TagCount]: ..." + new_methods)

with open("coppermind/store_protocol.py", "w") as f:
    f.write(content)


with open("coppermind/store_client.py", "r") as f:
    content = f.read()

content = content.replace("ReplaceNote, TagCount,", "ReplaceNote, TagCount, SchemaResponse,")

new_methods = """
    async def get_schema(self) -> SchemaResponse:
        response = await self._send("GET", f"{INTERNAL_PREFIX}/schema")
        return SchemaResponse.model_validate(response.json())
"""
content = content.replace("return [TagCount.model_validate(t) for t in response.json()]", "return [TagCount.model_validate(t) for t in response.json()]\n" + new_methods)

with open("coppermind/store_client.py", "w") as f:
    f.write(content)


with open("services/store/coppermind_store/notes.py", "r") as f:
    content = f.read()

content = content.replace("ReplaceNote, TagCount,", "ReplaceNote, TagCount, SchemaResponse,")

new_method = """
    async def get_schema(self) -> SchemaResponse:
        state = self.control.store.read("schema")
        doc = {k: v for k, v in state.body.items() if k != "revision"}
        tags = await self.get_tag_counts()
        return SchemaResponse(revision=state.revision, schema_doc=doc, tag_counts=tags)
"""
content = content.replace("return [TagCount(tag=row.tag, count=row.count) for row in rows]", "return [TagCount(tag=row.tag, count=row.count) for row in rows]\n" + new_method)

with open("services/store/coppermind_store/notes.py", "w") as f:
    f.write(content)


with open("services/store/coppermind_store/internal_api.py", "r") as f:
    content = f.read()

content = content.replace("ReplaceNote, TagCount,", "ReplaceNote, TagCount, SchemaResponse,")

new_method = """
@router.get("/schema", response_model=SchemaResponse)
async def get_schema(request: Request) -> SchemaResponse | JSONResponse:
    try:
        return await _store(request).get_schema()
    except StoreError as error:
        return _failure(error)
"""
content = content.replace("@router.get(\"/tags\"", new_method.strip() + "\n\n\n@router.get(\"/tags\"")

with open("services/store/coppermind_store/internal_api.py", "w") as f:
    f.write(content)

