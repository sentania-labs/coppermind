"""Matching helpers for voice signatures; vectors must stay in private storage."""

from __future__ import annotations

import math
from typing import Any


def cosine(left: list[float], right: list[float]) -> float | None:
    """Return cosine for two finite, nonzero vectors of the same dimension."""
    if not left or len(left) != len(right):
        return None
    if any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
        for x in (*left, *right)
    ):
        return None
    left_norm = math.hypot(*left)
    right_norm = math.hypot(*right)
    if not left_norm or not right_norm:
        return None
    return math.fsum((a / left_norm) * (b / right_norm) for a, b in zip(left, right, strict=True))


def matches(
    embeddings: list[dict[str, Any]],
    saved: list[dict[str, Any]],
    *,
    strong: float = 0.60,
    maybe: float = 0.45,
) -> list[dict[str, Any]]:
    """Return each person's best compatible match, without returning stored vectors."""
    if not -1 <= maybe <= strong <= 1:
        raise ValueError("voice thresholds must satisfy -1 <= maybe <= strong <= 1")
    best: dict[str, float] = {}
    for embedding in embeddings:
        for signature in saved:
            if (
                not isinstance(embedding.get("model"), str)
                or not isinstance(embedding.get("version"), str)
                or (embedding["model"], embedding["version"])
                != (signature.get("model"), signature.get("version"))
            ):
                continue
            left, right = embedding.get("vector"), signature.get("vector")
            person = signature.get("person_id")
            if (
                not isinstance(left, list)
                or not isinstance(right, list)
                or not isinstance(person, str)
            ):
                continue
            score = cosine(left, right)
            if score is not None and score >= maybe:
                best[person] = max(score, best.get(person, -math.inf))
    return [
        {"person_id": person, "score": score, "strength": "strong" if score >= strong else "maybe"}
        for person, score in sorted(best.items(), key=lambda item: (-item[1], item[0]))
    ]
