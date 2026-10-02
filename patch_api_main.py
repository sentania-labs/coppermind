import re

with open("services/api/coppermind_api/main.py", "r") as f:
    content = f.read()

content = content.replace("from coppermind_api.v1.sources import router as sources_router", "from coppermind_api.v1.sources import router as sources_router\nfrom coppermind_api.v1.schema import router as schema_router")
content = content.replace("app.include_router(notes_router)", "app.include_router(notes_router)\n    app.include_router(schema_router)")

with open("services/api/coppermind_api/main.py", "w") as f:
    f.write(content)
