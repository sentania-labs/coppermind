"""Read, edit and write the YAML frontmatter of a note file.

Every note is a Markdown file whose first block is YAML between two `---`
lines. Editing it is done in ruamel.yaml round trip mode, which preserves key
order, comments and quoting style. That matters operationally: a plain YAML
load and dump would reorder every key in every file the system touches, and
Obsidian Sync would then push a whole rewritten notes filesystem to every
device. A minimal diff keeps a system write to the keys it actually changed.

That minimal diff assumes ordinary line endings. The block is reassembled from
the round trip dump, which emits line feeds, so a frontmatter block written
with carriage returns comes back entirely in line feeds and syncs whole. The
body keeps its own line endings either way. Preserving the block's line
endings here is a follow-up, because it changes the whole document replace as
well as the frontmatter patch.

Round trip mode does not preserve the source's indentation either, so a
targeted `patch` asks the loader to guess the block's own indentation and
writes it back that way. A whole document `compose`, which builds the block
from what was sent rather than from the file, keeps the house style.

Unknown keys are never removed. Anything a person or another tool put in the
frontmatter survives a Coppermind write untouched.
"""

from __future__ import annotations

import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.util import load_yaml_guess_indent

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
_ITEM_WITH_NO_VALUE = re.compile(r"^([ \t]*-)[ \t]*$", re.MULTILINE)
_LONE_CARRIAGE_RETURN = re.compile(r"\r(?!\n)")


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
    block, body, _ = _split(text)
    return block, body


def _split(text: str) -> tuple[str, str, int]:
    """Split note text and report the length of its opening delimiter line.

    The length is negative when the file carries no frontmatter at all. It is
    the one answer about the opening line, so nothing downstream has to decide
    a second time which line endings a block opened with and disagree.
    """
    if not text.startswith(DELIMITER):
        return "", text, -1
    rest = text[len(DELIMITER) :]
    if rest[:1] not in {"\n", "\r"}:
        return "", text, -1
    rest = rest.lstrip("\r").removeprefix("\n")
    opening_length = len(text) - len(rest)
    end = _find_closing_delimiter(rest)
    if end is None:
        raise FrontmatterError("frontmatter block is never closed", category="unterminated_block")
    block = rest[:end]
    after = rest[end:]
    after = after.split("\n", 1)[1] if "\n" in after else ""
    return block, after, opening_length


def _find_closing_delimiter(text: str) -> int | None:
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.rstrip("\r\n") == DELIMITER:
            return offset
        offset += len(line)
    return None


def _unparseable(exc: YAMLError) -> FrontmatterError:
    mark = getattr(exc, "problem_mark", None)
    return FrontmatterError(
        str(exc),
        category=_category(exc),
        line=getattr(mark, "line", None),
        column=getattr(mark, "column", None),
    )


def _mapping(loaded: Any) -> dict[str, Any]:
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise FrontmatterError("frontmatter is not a mapping", category="not_a_mapping")
    return loaded


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Return the frontmatter mapping and the body of a note file."""
    block, body = split(text)
    if not block.strip():
        return {}, body
    try:
        loaded = _yaml().load(block)
    except YAMLError as exc:
        raise _unparseable(exc) from exc
    return _mapping(loaded), body


def _indent_of(block: str) -> tuple[int | None, int | None]:
    """The loader's guess for a block, or no guess when it cannot be made."""
    try:
        _, indent, sequence_offset = load_yaml_guess_indent(block, yaml=_yaml())
    except (YAMLError, IndexError):
        return None, None
    return indent, sequence_offset


def _load_guessing_indent(block: str, yaml: YAML) -> tuple[Any, int | None, int | None]:
    """Load a block and name the indentation it already uses.

    The guess walks the raw lines before anything is parsed, and that walk is
    not total: a list item a person left blank has no value to measure and
    runs the walk off the end of its line. Lending that one line a value keeps
    the rest of the block measurable, so a blank entry left on a phone does
    not cost the note the style of every list in it.
    """
    try:
        try:
            return load_yaml_guess_indent(block, yaml=yaml)
        except IndexError:
            return (yaml.load(block), *_indent_of(_ITEM_WITH_NO_VALUE.sub(r"\1 x", block)))
    except YAMLError as exc:
        raise _unparseable(exc) from exc


def _indent_like(yaml: YAML, indent: int | None, sequence_offset: int | None) -> None:
    """Write a block back at the indentation the file already uses.

    The loader's own guess names the indentation of the block's first list,
    or, when it has no list, the nesting of its mappings. A block that has
    both keeps its list style and takes the house nesting for its mappings.
    Anything the guess cannot name, and anything a dump cannot express, keeps
    the house style.
    """
    if indent is None or indent < 2:
        return
    if sequence_offset is None:
        yaml.indent(mapping=indent)
    elif 0 <= sequence_offset <= indent - 2:
        yaml.indent(sequence=indent, offset=sequence_offset)


def _dump(frontmatter: dict[str, Any], yaml: YAML) -> str:
    if not frontmatter:
        return ""
    stream = io.StringIO()
    yaml.dump(frontmatter, stream)
    return stream.getvalue()


def dump(frontmatter: dict[str, Any]) -> str:
    """Serialise a frontmatter mapping to its YAML block, without delimiters."""
    return _dump(frontmatter, _yaml())


def _compose(frontmatter: dict[str, Any], body: str, yaml: YAML) -> str:
    block = _dump(frontmatter, yaml)
    if not block:
        return body
    if not block.endswith("\n"):
        block += "\n"
    return f"{DELIMITER}\n{block}{DELIMITER}\n{body}"


def compose(frontmatter: dict[str, Any], body: str) -> str:
    """Build note file text from a frontmatter mapping and a body."""
    return _compose(frontmatter, body, _yaml())


def patch(text: str, changes: dict[str, Any], *, unset: list[str] | None = None) -> str:
    """Apply `changes` to the frontmatter of `text` and return the new text.

    Only the touched keys move. A key that is already present keeps its
    position; a new key is appended after the existing ones. A file with no
    frontmatter gains a block. The block is written back at its own
    indentation, so a list written flush with its key stays flush rather than
    syncing to every device as a rewritten list. A block that mixes a list
    with mappings nested at another width keeps the list style and takes the
    house nesting for those mappings.
    """
    block, body = split(text)
    yaml = _yaml()
    frontmatter: dict[str, Any] = {}
    if block.strip():
        loaded, indent, sequence_offset = _load_guessing_indent(block, yaml)
        frontmatter = _mapping(loaded)
        _indent_like(yaml, indent, sequence_offset)
    for key in unset or []:
        frontmatter.pop(key, None)
    for key, value in changes.items():
        frontmatter[key] = value
    return _compose(frontmatter, body, yaml)


def append_missing(text: str, changes: dict[str, Any]) -> str:
    """Append absent keys without rewriting any existing frontmatter bytes.

    Adoption adds system-owned defaults to a file a person wrote. The whole
    existing block is still parsed in round trip mode, but only the generated
    additions are dumped. They are spliced immediately before the closing
    delimiter using the block's line endings, so comments, quoting,
    indentation, ordering and the body remain byte exact.

    A file whose lines end in a bare carriage return is refused rather than
    written. `split` terminates the block on a line feed, so it reports no body
    for such a file, and adopting one would mirror and serve it empty. Reading
    those endings is a correction to the shared parser, not to adoption.
    """
    if _LONE_CARRIAGE_RETURN.search(text):
        raise FrontmatterError(
            "note uses carriage return line endings",
            category="unsupported_line_endings",
        )
    block, body, opening_length = _split(text)
    if opening_length < 0:
        return compose(changes, body)
    yaml = _yaml()
    if block.strip():
        loaded, indent, sequence_offset = _load_guessing_indent(block, yaml)
        frontmatter = _mapping(loaded)
        _indent_like(yaml, indent, sequence_offset)
    else:
        frontmatter = {}
    additions = {key: value for key, value in changes.items() if key not in frontmatter}
    if not additions:
        return text
    fragment = _dump(additions, yaml)
    newline = text[len(DELIMITER) : opening_length]
    fragment = fragment.replace("\n", newline)
    closing_offset = _find_closing_delimiter(text[opening_length:])
    if closing_offset is None:  # split already checked this; keeps the invariant local
        raise FrontmatterError("frontmatter block is never closed", category="unterminated_block")
    insertion = opening_length + closing_offset
    return f"{text[:insertion]}{fragment}{text[insertion:]}"
