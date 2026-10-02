import re

with open("coppermind/store_protocol.py", "r") as f:
    content = f.read()

new_classes = """

class TagCount(BaseModel):
    tag: str
    count: int

class SchemaResponse(BaseModel):
    revision: int
    schema_doc: dict[str, Any]
    tag_counts: list[TagCount]
"""

content = content.replace("class Page[T](BaseModel):", new_classes + "\nclass Page[T](BaseModel):")

new_methods = """
    async def get_schema(self) -> SchemaResponse: ...

    async def put_schema(self, schema_doc: dict[str, Any], if_revision: int) -> SchemaResponse: ...
"""

content = content.replace("async def get_api_keys(self) -> ApiKeySet: ...", "async def get_api_keys(self) -> ApiKeySet: ..." + new_methods)

with open("coppermind/store_protocol.py", "w") as f:
    f.write(content)
