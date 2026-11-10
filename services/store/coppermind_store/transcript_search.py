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


def _spans(transcript: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in pattern.finditer(transcript):
        start = max(0, match.start() - 300)
        end = min(len(transcript), match.end() + 300)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
        else:
            spans.append((start, end))
    return spans


def _pattern(terms: set[str]) -> re.Pattern[str]:
    ordered = sorted(terms, key=lambda x: (-len(x), x))
    return re.compile(
        "(?<!\\w)(?:" + "|".join(re.escape(term) for term in ordered) + ")(?!\\w)", re.I
    )


def passages(transcripts: list[str], terms: set[str], limit: int = 12000) -> tuple[list[str], int]:
    terms = {term.strip() for term in terms if 2 <= len(term.strip()) <= 200}
    total = sum(map(len, transcripts))
    if not terms:
        return [], total
    specific = {term for term in terms if "@" in term or len(term.split()) > 1}
    chosen: list[list[tuple[int, int]]] = [[] for _ in transcripts]
    used = 0
    for tier in (specific, terms - specific):
        if not tier:
            continue
        pattern = _pattern(tier)
        for index, transcript in enumerate(transcripts):
            for start, end in _spans(transcript, pattern):
                remaining = limit - used
                if remaining <= 0:
                    break
                end = min(end, start + remaining)
                chosen[index].append((start, end))
                used += end - start
    selected: list[str] = []
    for transcript, spans in zip(transcripts, chosen, strict=True):
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        selected.extend(transcript[start:end] for start, end in merged)
    return selected, total - sum(map(len, selected))
