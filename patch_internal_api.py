import re

with open("services/store/coppermind_store/internal_api.py", "r") as f:
    content = f.read()

content = content.replace("ReplaceNote,", "ReplaceNote, TagCount,")

new_method = """
@router.get("/tags", response_model=list[TagCount])
async def get_tag_counts(request: Request) -> list[TagCount] | JSONResponse:
    try:
        return await _store(request).get_tag_counts()
    except StoreError as error:
        return _failure(error)
"""
content = content.replace("@router.get(\"/api-keys\"", new_method.strip() + "\n\n\n@router.get(\"/api-keys\"")

with open("services/store/coppermind_store/internal_api.py", "w") as f:
    f.write(content)
