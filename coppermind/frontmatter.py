"""Read, edit and write the YAML frontmatter of a note file.

Every note is a Markdown file whose first block is YAML between two `---`
lines. Editing it is done in ruamel.yaml round trip mode, which preserves key
order, comments and quoting style. That matters operationally: a plain YAML
load and dump would reorder every key in every file the system touches, and
Obsidian Sync would then push a whole rewritten notes filesystem to every
device. A minimal diff keeps a system write to the keys it actually changed.

Unknown keys are never removed. Anything a person or another tool put in the
frontmatter survives a Coppermind write untouched.
"""

from __future__ import annotations

import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

DELIMITER = "---"


class FrontmatterError(ValueError):
    """The file does not carry parseable frontmatter.

    The message quotes the parser, which quotes the note, so it is the
    person's own content and belongs only where note content belongs.
    `category`, `line` and `column` are content free by construction, which
    makes them the parts an operational log may carry. The position is
    counted inside the frontmatter block, not the whole file.
    """

    def __init__(
        self,
        message: str,
        *,
        category: str,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.line = line
        self.column = column


_WORD_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _category(exc: YAMLError) -> str:
    """Name a parse failure from its type alone, so it cannot carry content."""
    return _WORD_BOUNDARY.sub("_", type(exc).__name__.removesuffix("Error")).lower()


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # never wrap a long value onto a second line
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def split(text: str) -> tuple[str, str]:
    """Split note text into its raw frontmatter block and its body.

    Returns ("", text) when the file has no frontmatter, which is the normal
    state of a note a person just created in Obsidian.
    """
    if not text.startswith(DELIMITER):
        return "", text
    rest = text[len(DELIMITER) :]
    if rest[:1] not in {"\n", "\r"}:
        return "", text
    rest = rest.lstrip("\r").removeprefix("\n")
    end = _find_closing_delimiter(rest)
    if end is None:
        raise FrontmatterError("frontmatter block is never closed", category="unterminated_block")
    block = rest[:end]
    after = rest[end:]
    after = after.split("\n", 1)[1] if "\n" in after else ""
    return block, after


def _find_closing_delimiter(text: str) -> int | None:
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.rstrip("\r\n") == DELIMITER:
            return offset
        offset += len(line)
    return None


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Return the frontmatter mapping and the body of a note file."""
    block, body = split(text)
    if not block.strip():
        return {}, body
    try:
        loaded = _yaml().load(block)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise FrontmatterError(
            str(exc),
            category=_category(exc),
            line=getattr(mark, "line", None),
            column=getattr(mark, "column", None),
        ) from exc
    if loaded is None:
        return {}, body
    if not isinstance(loaded, dict):
        raise FrontmatterError("frontmatter is not a mapping", category="not_a_mapping")
    return loaded, body


def dump(frontmatter: dict[str, Any]) -> str:
    """Serialise a frontmatter mapping to its YAML block, without delimiters."""
    if not frontmatter:
        return ""
    stream = io.StringIO()
    _yaml().dump(frontmatter, stream)
    return stream.getvalue()


def compose(frontmatter: dict[str, Any], body: str) -> str:
    """Build note file text from a frontmatter mapping and a body."""
    block = dump(frontmatter)
    if not block:
        return body
    if not block.endswith("\n"):
        block += "\n"
    return f"{DELIMITER}\n{block}{DELIMITER}\n{body}"


def patch(text: str, changes: dict[str, Any], *, unset: list[str] | None = None) -> str:
    """Apply `changes` to the frontmatter of `text` and return the new text.

    Only the touched keys move. A key that is already present keeps its
    position; a new key is appended after the existing ones. A file with no
    frontmatter gains a block.
    """
    frontmatter, body = parse(text)
    for key in unset or []:
        frontmatter.pop(key, None)
    for key, value in changes.items():
        frontmatter[key] = value
    return compose(frontmatter, body)
