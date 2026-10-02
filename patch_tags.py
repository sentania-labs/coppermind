import re

with open("coppermind/store_protocol.py", "r") as f:
    content = f.read()

new_classes = """

class TagCount(BaseModel):
    tag: str
    count: int
"""
content = content.replace("class Page[T](BaseModel):", new_classes + "\nclass Page[T](BaseModel):")

new_methods = """
    async def get_tag_counts(self) -> list[TagCount]: ...
"""
content = content.replace("async def get_api_keys(self) -> ApiKeySet: ...", "async def get_api_keys(self) -> ApiKeySet: ..." + new_methods)

with open("coppermind/store_protocol.py", "w") as f:
    f.write(content)

with open("coppermind/store_client.py", "r") as f:
    content = f.read()

content = content.replace("ReplaceNote,", "ReplaceNote, TagCount,")

new_methods = """
    async def get_tag_counts(self) -> list[TagCount]:
        response = await self._send("GET", f"{INTERNAL_PREFIX}/tags")
        return [TagCount.model_validate(t) for t in response.json()]
"""
content = content.replace("async def get_api_keys(self) -> ApiKeySet:", new_methods.strip() + "\n\n    async def get_api_keys(self) -> ApiKeySet:")

with open("coppermind/store_client.py", "w") as f:
    f.write(content)
