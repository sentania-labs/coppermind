import re

with open("coppermind/schema.py", "r") as f:
    content = f.read()

new_validate = """    def validate_frontmatter(self, frontmatter: dict[str, Any]) -> list[str]:
        \"\"\"Return a list of human readable problems, empty when the note is valid.

        A problem quotes the note's own value, so it belongs where note content
        belongs: an answer to whoever sent the note, never an operational log.
        `invalid_keys` is the form that may be logged.

        Unknown keys are not problems. They are passed through untouched, so a
        person can keep their own keys in a note without Coppermind objecting.
        \"\"\"
        problems = [problem for _, problem in self._problems(frontmatter)]
        tags_key = self.roles.get("tags_key")
        if tags_key in frontmatter and not self.tags.open:
            tags = frontmatter.get(tags_key)
            if isinstance(tags, list):
                for tag in tags:
                    if isinstance(tag, str) and tag not in self.tags.meanings and tag not in self.tags.aliases:
                        problems.append(f"{tags_key}: closed tags policy refuses unknown tag {tag!r}")
        return problems
"""
# Need to replace exactly the validate_frontmatter method
content = re.sub(r'    def validate_frontmatter\(self, frontmatter: dict\[str, Any\]\) -> list\[str\]:.*?(?=    def invalid_keys)', new_validate, content, flags=re.DOTALL)

new_method = """
    def normalize_tags_in_place(self, frontmatter: dict[str, Any]) -> None:
        tags_key = self.roles.get("tags_key")
        if tags_key in frontmatter:
            tags = frontmatter[tags_key]
            if isinstance(tags, list):
                new_tags = []
                for tag in tags:
                    if not isinstance(tag, str):
                        new_tags.append(tag)
                        continue
                    canonical = self.tags.aliases.get(tag, tag)
                    if canonical not in new_tags:
                        new_tags.append(canonical)
                frontmatter[tags_key] = new_tags
"""
content = content.replace("    def validate_frontmatter(", new_method.strip() + "\n\n    def validate_frontmatter(")

with open("coppermind/schema.py", "w") as f:
    f.write(content)
