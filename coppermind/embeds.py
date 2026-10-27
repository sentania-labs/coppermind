"""Rewrite note image embeds that name a source artifact to its filed attachment path."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _outside_fences(body: str):
    """Yield (text, in_fence) chunks so authored code samples are never rewritten."""
    fence: str | None = None
    chunk: list[str] = []
    for line in body.splitlines(keepends=True):
        match = _FENCE.match(line)
        closes = (
            fence is not None
            and match is not None
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence)
            and line.strip() == match.group(1)
        )
        if closes:
            chunk.append(line)
            yield "".join(chunk), True
            chunk, fence = [], None
        elif fence is None and match:
            if chunk:
                yield "".join(chunk), False
            chunk, fence = [line], match.group(1)
        else:
            chunk.append(line)
    if chunk:
        yield "".join(chunk), fence is not None


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
    markdown = re.compile(r"(!\[[^\]\n]*\]\()([^()\s<>]+)(\))")

    def wiki_sub(match: re.Match[str]) -> str:
        filed = resolve(match.group(1))
        return match.group(0) if filed is None else f"![[{filed}{match.group(2)}]]"

    def markdown_sub(match: re.Match[str]) -> str:
        filed = resolve(unquote(match.group(2)))
        if filed is None:
            return match.group(0)
        return f"{match.group(1)}{quote(filed, safe='/')}{match.group(3)}"

    out = []
    for text, in_fence in _outside_fences(body):
        out.append(text if in_fence else markdown.sub(markdown_sub, wiki.sub(wiki_sub, text)))
    return "".join(out)
