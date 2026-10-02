import re

with open("services/admin/coppermind_admin/main.py", "r") as f:
    content = f.read()

content = content.replace("from coppermind_admin.pages import keys", "from coppermind_admin.pages import keys\n    from coppermind_admin.pages import fields")
content = content.replace("app.include_router(keys.router)", "app.include_router(keys.router)\n    app.include_router(fields.router)")

with open("services/admin/coppermind_admin/main.py", "w") as f:
    f.write(content)
