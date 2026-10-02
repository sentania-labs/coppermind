import re

with open("services/store/coppermind_store/notes.py", "r") as f:
    content = f.read()

content = content.replace(
    "frontmatter = _build_frontmatter(request, schema, settings, note_id)\n        problems = schema.validate_frontmatter(frontmatter)",
    "frontmatter = _build_frontmatter(request, schema, settings, note_id)\n        schema.normalize_tags_in_place(frontmatter)\n        problems = schema.validate_frontmatter(frontmatter)"
)

content = content.replace(
    "sent = _replacement_frontmatter(request, schema, note_id)\n        problems = schema.validate_frontmatter(sent)",
    "sent = _replacement_frontmatter(request, schema, note_id)\n        schema.normalize_tags_in_place(sent)\n        problems = schema.validate_frontmatter(sent)"
)

content = content.replace(
    "adopted_frontmatter, adopted_body = fm.parse(adopted_text)\n        except fm.FrontmatterError",
    "adopted_frontmatter, adopted_body = fm.parse(adopted_text)\n            schema.normalize_tags_in_place(adopted_frontmatter)\n            adopted_text = fm.compose(adopted_frontmatter, adopted_body)\n        except fm.FrontmatterError"
)

content = content.replace(
    "id_key = schema.role(\"id_key\")\n            if frontmatter.get(id_key) != current_frontmatter.get(id_key):",
    "schema.normalize_tags_in_place(frontmatter)\n            id_key = schema.role(\"id_key\")\n            if frontmatter.get(id_key) != current_frontmatter.get(id_key):"
)

with open("services/store/coppermind_store/notes.py", "w") as f:
    f.write(content)
