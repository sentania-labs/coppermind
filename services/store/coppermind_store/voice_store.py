"""Private voice signatures and per-person consent.

Callers serialize mutations with the context service lock.  This file is not a
vault artifact and its vectors must never be included in API representations.
"""

from __future__ import annotations

import math
from typing import Any

from coppermind_store.voice_matching import cosine, matches


class VoiceStore:
    def __init__(self, context: Any) -> None:
        self.context = context

    def _load(self) -> dict[str, Any]:
        return self.context.read("voice-signatures", {"people": {}})

    def _save(self, data: dict[str, Any]) -> None:
        self.context.write("voice-signatures", data)

    def reject(self, note_id: str, label: str, person_id: str) -> None:
        data = self._load()
        record = {"note_id": note_id, "label": label, "person_id": person_id}
        rejections = data.setdefault("rejections", [])
        if record not in rejections:
            rejections.append(record)
            self._save(data)

    def rejected(self, note_id: str, label: str, person_id: str) -> bool:
        return {"note_id": note_id, "label": label, "person_id": person_id} in self._load().get(
            "rejections", []
        )

    @staticmethod
    def _summary(person: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": person.get("enabled", False),
            "signatures": [
                {key: entry[key] for key in ("note_id", "created_at", "speech_seconds")}
                for entry in person.get("signatures", [])
            ],
        }

    def summary(self, person_id: str) -> dict[str, Any]:
        return self._summary(self._load()["people"].get(person_id, {}))

    def summaries(self) -> dict[str, dict[str, Any]]:
        return {pid: self._summary(person) for pid, person in self._load()["people"].items()}

    def set_enabled(self, person_id: str, enabled: bool) -> None:
        data = self._load()
        person = data["people"].setdefault(person_id, {"signatures": []})
        person["enabled"] = enabled
        if not enabled:
            self._clear(data, person_id)
        self._save(data)

    @staticmethod
    def _clear(data: dict[str, Any], person_id: str) -> None:
        if person_id in data["people"]:
            data["people"][person_id]["signatures"] = []
        data["rejections"] = [r for r in data.get("rejections", []) if r["person_id"] != person_id]

    def delete(self, person_id: str) -> None:
        data = self._load()
        self._clear(data, person_id)
        self._save(data)

    def purge(self, person_ids: set[str], note_ids: set[str]) -> None:
        data = self._load()
        for person_id in person_ids:
            data["people"].pop(person_id, None)
        for person in data["people"].values():
            person["signatures"] = [
                signature
                for signature in person.get("signatures", [])
                if signature["note_id"] not in note_ids
            ]
        data["rejections"] = [
            r
            for r in data.get("rejections", [])
            if r["person_id"] not in person_ids and r["note_id"] not in note_ids
        ]
        self._save(data)

    def confirm(
        self,
        person_id: str,
        note_id: str,
        created_at: str,
        speech_seconds: float,
        embeddings: list[dict[str, Any]],
    ) -> None:
        data = self._load()
        person = data["people"].get(person_id, {})
        if not person.get("enabled"):
            return
        if (
            isinstance(speech_seconds, bool)
            or not isinstance(speech_seconds, (int, float))
            or not math.isfinite(speech_seconds)
            or speech_seconds < 0
        ):
            return
        signatures = person.setdefault("signatures", [])
        if not isinstance(embeddings, list):
            return
        for embedding in embeddings:
            if not isinstance(embedding, dict):
                continue
            model, version, vector = (embedding.get(k) for k in ("model", "version", "vector"))
            if (
                not isinstance(model, str)
                or not model
                or not isinstance(version, str)
                or not version
                or not isinstance(vector, list)
                or cosine(vector, vector) is None
            ):
                continue
            if any(
                s["note_id"] == note_id and s["model"] == model and s["version"] == version
                for s in signatures
            ):
                continue
            signatures.append(
                {
                    "note_id": note_id,
                    "created_at": created_at,
                    "speech_seconds": speech_seconds,
                    "model": model,
                    "version": version,
                    "vector": vector,
                }
            )
        self._save(data)

    def match(
        self, embeddings: list[dict[str, Any]], *, strong: float = 0.60, maybe: float = 0.45
    ) -> list[dict[str, Any]]:
        data = self._load()
        saved = [
            dict(signature, person_id=person_id)
            for person_id, person in data["people"].items()
            if person.get("enabled")
            for signature in person.get("signatures", [])
        ]
        return matches(embeddings, saved, strong=strong, maybe=maybe)
