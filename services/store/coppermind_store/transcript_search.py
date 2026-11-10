"""Select small, deterministic transcript passages around known meeting entities."""

import re
from typing import Any


def search_terms(
    frontmatter: dict[str, Any],
    fields: set[str],
    events: list[dict[str, Any]],
    dossiers: list[dict[str, Any]],
) -> set[str]:
    terms: set[str] = set()
    for key in fields:
        value = frontmatter.get(key)
        values = value if isinstance(value, list) else [value]
        terms.update(item for item in values if isinstance(item, str) and item.strip())
    for event in events:
        for person in event["people"]:
            terms.update(person.get(key, "") for key in ("name", "email") if person.get(key))
            address = person.get("address", "")
            if isinstance(address, str) and address:
                terms.add(address.removeprefix("mailto:"))
    for dossier in dossiers:
        terms.update([dossier["name"], *dossier["aliases"], *dossier["emails"]])
    return terms


def passages(transcripts: list[str], terms: set[str], limit: int = 12000) -> tuple[list[str], int]:
    terms = {term.strip() for term in terms if 2 <= len(term.strip()) <= 200}
    if not terms:
        return [], sum(map(len, transcripts))
    pattern = re.compile(
        "|".join(re.escape(term) for term in sorted(terms, key=lambda x: (-len(x), x))), re.I
    )
    selected: list[str] = []
    total = sum(map(len, transcripts))
    used = 0
    for transcript in transcripts:
        spans: list[tuple[int, int]] = []
        for match in pattern.finditer(transcript):
            start = max(0, match.start() - 300)
            end = min(len(transcript), match.end() + 300)
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
            else:
                spans.append((start, end))
        for start, end in spans:
            remaining = limit - used
            if remaining <= 0:
                return selected, total - used
            part = transcript[start : min(end, start + remaining)]
            selected.append(part)
            used += len(part)
    return selected, total - used
