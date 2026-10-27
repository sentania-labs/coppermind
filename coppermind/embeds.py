"""Rewrite note image embeds that name a source artifact to its filed attachment path."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

# A fence may sit inside blockquotes and list items, so allow their markers before it.
_PREFIX = r"[ \t]*(?:>[ \t]*)*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*"
_FENCE = re.compile(rf"^{_PREFIX}(`{{3,}}|~{{3,}})(.*)$")
# CommonMark allows balanced parentheses in an unbracketed destination; three levels is plenty.
_DESTINATION = r"(?:[^()\s<>]|\((?:[^()\s<>]|\((?:[^()\s<>]|\([^()\s<>]*\))*\))*\))+"
_BACKTICKS = re.compile(r"`+")
_MARKER = re.compile(r"^ *([-*+]|\d{1,9}[.)])( +|$)")


def _code_spans_split(text: str):
    """Yield (text, is_code) pieces of one paragraph, splitting out inline code spans."""
    start = 0
    runs = list(_BACKTICKS.finditer(text))
    index = 0
    while index < len(runs):
        opener = runs[index]
        closer = next(
            (later for later in runs[index + 1 :] if len(later.group()) == len(opener.group())),
            None,
        )
        if closer is None:
            index += 1
            continue
        if opener.start() > start:
            yield text[start : opener.start()], False
        yield text[opener.start() : closer.end()], True
        start = closer.end()
        index = runs.index(closer) + 1
    if start < len(text):
        yield text[start:], False


def _prose_and_code(body: str):
    """Yield (text, is_code) so authored code is never treated as an embed.

    Code is fenced blocks (also inside blockquotes and lists), indented code
    blocks, and inline code spans. Following CommonMark, an indented line is
    code only when it is four columns past its container, so list item content
    and its continuation lines stay prose.
    """
    fence: re.Match[str] | None = None
    prose: list[str] = []
    items: list[int] = []
    previous_blank = True
    previous_code = False

    def flush():
        text = "".join(prose)
        prose.clear()
        for paragraph in re.split(r"(?<=\n)(?=[ \t]*\n)", text):
            yield from _code_spans_split(paragraph)

    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if fence is not None:
            yield line, True
            match = _FENCE.match(stripped)
            if (
                match
                and match.group(1)[0] == fence.group(1)[0]
                and len(match.group(1)) >= len(fence.group(1))
                and not match.group(2).strip()
            ):
                fence = None
            previous_blank, previous_code = False, True
            continue
        if not stripped.strip():
            prose.append(line)
            previous_blank, previous_code = True, False
            continue
        measured = stripped.expandtabs(4)
        indent = len(measured) - len(measured.lstrip(" "))
        marker = _MARKER.match(measured)
        lazy = bool(items) and not marker and not previous_blank and not previous_code
        while items and indent < items[-1] and not lazy:
            items.pop()
        if indent - (items[-1] if items else 0) >= 4 and (previous_blank or previous_code):
            yield from flush()
            yield line, True
            previous_blank, previous_code = False, True
            continue
        if marker:
            spaces = len(marker.group(2))
            width = marker.end() if 1 <= spaces <= 4 else marker.end(1) + 1
            items.append(width)
        opening = _FENCE.match(stripped)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            yield from flush()
            fence = opening
            yield line, True
            previous_blank, previous_code = False, True
            continue
        prose.append(line)
        previous_blank, previous_code = False, False
    yield from flush()


def rewrite_embeds(
    body: str,
    attachments: list[tuple[str, str, str]],
    *,
    attachments_folder: str,
) -> str:
    """Point `![[name]]` and `![alt](name)` at the filed attachment for `name`.

    `attachments` holds `(source_id, artifact name, filed path)` for the current
    revision of each cited source. Only a target that is exactly an artifact
    name, or the filed path of an earlier revision of that same artifact, is
    changed, so refiling never rewrites twice and every other link stays as
    authored.
    """
    exact = {name: filed for _, name, filed in attachments}
    earlier = [
        (
            re.compile(
                rf"{re.escape(attachments_folder)}/\d{{4}}-\d{{2}}/"
                rf"{re.escape(source_id)}-r\d{{4}}-{re.escape(name)}"
            ),
            filed,
        )
        for source_id, name, filed in attachments
    ]

    def resolve(target: str) -> str | None:
        if target in exact:
            return exact[target]
        return next((filed for rx, filed in earlier if rx.fullmatch(target)), None)

    wiki = re.compile(r"!\[\[([^\]|\n]+)((?:\|[^\]\n]*)?)\]\]")
    markdown = re.compile(
        rf"(!\[[^\]\n]*\]\([ \t]*)(<[^<>\n]+>|{_DESTINATION})"
        r"((?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*\))"
    )

    def wiki_sub(match: re.Match[str]) -> str:
        filed = resolve(match.group(1))
        return match.group(0) if filed is None else f"![[{filed}{match.group(2)}]]"

    def markdown_sub(match: re.Match[str]) -> str:
        target = match.group(2)
        angled = target.startswith("<")
        filed = resolve(unquote(target[1:-1] if angled else target))
        if filed is None:
            return match.group(0)
        written = f"<{filed}>" if angled else quote(filed, safe="/")
        return f"{match.group(1)}{written}{match.group(3)}"

    out = []
    for text, is_code in _prose_and_code(body):
        out.append(text if is_code else markdown.sub(markdown_sub, wiki.sub(wiki_sub, text)))
    return "".join(out)
