"""Private voice signatures and recording-level speaker assignments.

Callers serialize mutations with the context service lock.  This file is not a
vault artifact and its vectors must never be included in API representations.
"""

from __future__ import annotations

import math
from typing import Any

from coppermind_store.voice_matching import cosine, matches


def _same_recording(entry: dict[str, Any], source_id: str | None) -> bool:
    return not source_id or not entry.get("source_id") or entry["source_id"] == source_id


def _attributed(data: dict[str, Any]) -> dict[str, Any]:
    """Tag older merged-note rows with the one recording whose assignment they belong to."""
    owners: dict[tuple[str, str], set[str]] = {}
    for assignment in data.get("assignments", []):
        if assignment.get("source_id"):
            owners.setdefault((assignment["note_id"], assignment["label"]), set()).add(
                assignment["source_id"]
            )
    rows = list(data.get("rejections", []))
    rows += [s for voice in data.get("unknowns", {}).values() for s in voice.get("signatures", [])]
    for row in rows:
        found = owners.get((row.get("note_id"), row.get("label")), set())
        if not row.get("source_id") and len(found) == 1:
            row["source_id"] = next(iter(found))
    return data


class VoiceStore:
    def __init__(self, context: Any) -> None:
        self.context = context

    def _load(self) -> dict[str, Any]:
        return _attributed(self.context.read("voice-signatures", {"people": {}}))

    def _save(self, data: dict[str, Any]) -> None:
        self.context.write("voice-signatures", data)

    def recognition_enabled(self) -> bool:
        return self._load().get("recognition_enabled", True)

    def set_recognition_enabled(self, enabled: bool) -> None:
        data = self._load()
        data["recognition_enabled"] = enabled
        self._save(data)
        if not enabled:
            for path in (self.context.root / "voice-pending").glob("*.json"):
                path.unlink(missing_ok=True)

    def capture_unknown(
        self,
        note_id: str,
        label: str,
        created_at: str,
        speech_seconds: float,
        embeddings: list[dict[str, Any]],
    ) -> str | None:
        """Remember a speaker, reusing only an unambiguous strong unknown match."""
        data = self._load()
        if not data.get("recognition_enabled", True):
            return None
        unknowns = data.setdefault("unknowns", {})
        for assignment in data.get("assignments", []):
            if assignment["note_id"] == note_id and assignment["label"] == label:
                return assignment["voice_id"]
        for identity, voice in unknowns.items():
            if any(
                s.get("note_id") == note_id and s.get("label") == label
                for s in voice.get("signatures", [])
            ):
                return identity
        if not isinstance(embeddings, list):
            embeddings = []
        saved = [
            dict(sig, person_id=identity)
            for identity, voice in unknowns.items()
            for sig in voice.get("signatures", [])
            if not any(s.get("note_id") == note_id for s in voice.get("signatures", []))
        ]
        settings = self.context.read("voice-settings", {"strong": 0.60, "maybe": 0.45})
        results = matches(embeddings, saved, strong=settings["strong"], maybe=settings["maybe"])
        strong = [item for item in results if item["strength"] == "strong"]
        if len(strong) == 1:
            identity = strong[0]["person_id"]
        else:
            serial = data.get("next_unknown", 1)
            identity = f"unknown:{serial}"
            data["next_unknown"] = serial + 1
            unknowns[identity] = {"name": f"Unknown voice {serial}", "signatures": []}
        if (
            isinstance(speech_seconds, bool)
            or not isinstance(speech_seconds, (int, float))
            or not math.isfinite(speech_seconds)
            or speech_seconds < 0
        ):
            speech_seconds = 0
        valid = []
        for embedding in embeddings if isinstance(embeddings, list) else []:
            if not isinstance(embedding, dict):
                continue
            model, version, vector = (embedding.get(k) for k in ("model", "version", "vector"))
            if (
                isinstance(model, str)
                and model
                and isinstance(version, str)
                and version
                and isinstance(vector, list)
                and cosine(vector, vector) is not None
            ):
                valid.append(
                    {
                        "note_id": note_id,
                        "label": label,
                        "created_at": created_at,
                        "speech_seconds": speech_seconds,
                        "model": model,
                        "version": version,
                        "vector": vector,
                    }
                )
        if not valid:
            return None
        unknowns[identity]["signatures"].extend(valid)
        data.setdefault("assignments", []).append(
            {"note_id": note_id, "label": label, "voice_id": identity, "origin_id": identity}
        )
        self._save(data)
        return identity

    def bind_recording(self, provisional_id: str, note_id: str) -> None:
        data = self._load()
        for voice in data.get("unknowns", {}).values():
            for signature in voice.get("signatures", []):
                if signature.get("note_id") == provisional_id:
                    signature["note_id"] = note_id
        for assignment in data.get("assignments", []):
            if assignment["note_id"] == provisional_id:
                assignment["note_id"] = note_id
        self._save(data)

    def reparent_recording(self, old_note_id: str, note_id: str, source_id: str) -> None:
        """Keep all voice data reachable after a recording note is merged."""
        data = self._load()
        for group in (data.get("people", {}), data.get("unknowns", {})):
            for voice in group.values():
                for signature in voice.get("signatures", []):
                    if signature.get("note_id") == old_note_id:
                        signature["note_id"] = note_id
                        signature["source_id"] = source_id
        for collection in ("assignments", "rejections", "deferred"):
            for entry in data.get(collection, []):
                if entry.get("note_id") == old_note_id:
                    entry["note_id"] = note_id
                    if collection in {"assignments", "rejections"}:
                        entry["source_id"] = source_id
                    if collection == "deferred" and isinstance(entry.get("assignment"), dict):
                        entry["assignment"]["note_id"] = note_id
                        entry["assignment"]["source_id"] = source_id
        self._save(data)

    def unknown_summaries(self) -> dict[str, dict[str, Any]]:
        """Return source information without exposing biometric vectors."""
        data = self._load()
        return {
            identity: {
                "name": voice["name"],
                "signatures": [
                    {
                        key: entry[key]
                        for key in ("note_id", "label", "created_at", "speech_seconds")
                    }
                    for entry in voice.get("signatures", [])
                ],
                "assignments": [
                    a for a in data.get("assignments", []) if a["voice_id"] == identity
                ],
            }
            for identity, voice in data.get("unknowns", {}).items()
        }

    def delete_unknown(self, identity: str) -> None:
        data = self._load()
        if identity not in data.get("unknowns", {}):
            raise ValueError("Unknown voice not found")
        assignments = [a for a in data.get("assignments", []) if a["voice_id"] == identity]
        self._clear_pending(assignments, data["unknowns"][identity]["name"])
        del data["unknowns"][identity]
        data["assignments"] = [a for a in data.get("assignments", []) if a["voice_id"] != identity]
        data["deferred"] = [
            item
            for item in data.get("deferred", [])
            if item.get("assignment", {}).get("voice_id") != identity
        ]
        self._save(data)

    def _clear_pending(self, assignments: list[dict[str, Any]], name: str) -> None:
        """Remove embeddings for these appearances, including merged recordings."""
        if self.context.recordings is not None:
            for recording in self.context.recordings.jobs():
                parent = getattr(self.context.recordings, "parent_note_id", lambda r: r.note_id)(
                    recording
                )
                matches = [
                    a
                    for a in assignments
                    if a["note_id"] in {parent, "recording:" + recording.id}
                    and (not a.get("source_id") or a["source_id"] == recording.source_id)
                ]
                if not matches:
                    continue
                key = "voice-pending/" + recording.id
                pending = self.context.read(key, [])
                labels = {a["label"] for a in matches} | {name}
                for speaker in pending:
                    if isinstance(speaker, dict) and speaker.get("label") in labels:
                        speaker.pop("embeddings", None)
                if pending:
                    self.context.write(key, pending)

    def assignments(self, voice_id: str) -> list[dict[str, Any]]:
        return [a for a in self._load().get("assignments", []) if a["voice_id"] == voice_id]

    def set_segments(self, note_id: str, transcript: dict[str, Any]) -> None:
        """Record original segment positions before any speaker labels are replaced."""
        data = self._load()
        for assignment in data.get("assignments", []):
            if assignment["note_id"] == note_id:
                assignment["segments"] = [
                    i
                    for i, segment in enumerate(transcript.get("segments", []))
                    if segment.get("speaker") == assignment["label"]
                ]
        self._save(data)

    def move_voice(self, source: str, target: str) -> list[dict[str, str]]:
        """Move an unknown voice's signatures and recording assignments."""
        data = self._load()
        unknowns = data.setdefault("unknowns", {})
        if source not in unknowns or source == target:
            raise ValueError("Unknown voice not found")
        if target.startswith("unknown:"):
            if target not in unknowns:
                raise ValueError("Target voice not found")
            destination = unknowns[target]
        elif target.startswith("person:") and target.removeprefix("person:"):
            destination = data["people"].setdefault(
                target.removeprefix("person:"), {"signatures": []}
            )
        else:
            raise ValueError("Target voice not found")
        destination.setdefault("signatures", []).extend(unknowns[source]["signatures"])
        del unknowns[source]
        moved = []
        for assignment in data.get("assignments", []):
            if assignment["voice_id"] == source:
                assignment["voice_id"] = target
                moved.append(dict(assignment))
        self._save(data)
        return moved

    def restore_unknown(self, identity: str) -> None:
        """Recover an earlier unknown group if it has since been attached elsewhere."""
        data = self._load()
        if identity in data.get("unknowns", {}):
            return
        assignments = [a for a in data.get("assignments", []) if a.get("origin_id") == identity]
        if not assignments or not identity.startswith("unknown:"):
            raise ValueError("This voice is no longer available. Refresh People and try again.")
        signatures = []
        for assignment in assignments:
            voice_id = assignment["voice_id"]
            source = (
                data.get("unknowns", {}).get(voice_id)
                if voice_id.startswith("unknown:")
                else data["people"].get(voice_id.removeprefix("person:"))
            )
            if source is None:
                raise ValueError("This voice is no longer available. Refresh People and try again.")
            selected = [
                s
                for s in source.get("signatures", [])
                if s.get("note_id") == assignment["note_id"]
                and s.get("label") == assignment["label"]
                and _same_recording(s, assignment.get("source_id"))
            ]
            signatures.extend(selected)
            source["signatures"] = [s for s in source.get("signatures", []) if s not in selected]
            assignment["voice_id"] = identity
        data.setdefault("unknowns", {})[identity] = {
            "name": "Unknown voice " + identity.removeprefix("unknown:"),
            "signatures": signatures,
        }
        self._save(data)

    def split_assignment(
        self, voice_id: str, note_id: str, label: str, source_id: str | None = None
    ) -> str:
        """Move one speaker appearance and its signatures to a new unknown voice."""
        data = self._load()
        assignment = next(
            (
                a
                for a in data.get("assignments", [])
                if (a["voice_id"], a["note_id"], a["label"], a.get("source_id"))
                == (voice_id, note_id, label, source_id)
            ),
            None,
        )
        if assignment is None:
            raise ValueError("Speaker assignment not found")
        if voice_id.startswith("unknown:"):
            source = data.get("unknowns", {}).get(voice_id)
        else:
            source = data["people"].get(voice_id.removeprefix("person:"))
        if source is None:
            raise ValueError("Voice not found")
        selected = [
            s
            for s in source.get("signatures", [])
            if s.get("note_id") == note_id
            and s.get("label") == label
            and _same_recording(s, source_id)
        ]
        serial = data.get("next_unknown", 1)
        identity = f"unknown:{serial}"
        data["next_unknown"] = serial + 1
        data.setdefault("unknowns", {})[identity] = {
            "name": f"Unknown voice {serial}",
            "signatures": selected,
        }
        source["signatures"] = [s for s in source.get("signatures", []) if s not in selected]
        assignment["voice_id"] = identity
        assignment["origin_id"] = identity
        self._save(data)
        return identity

    def reject(
        self, note_id: str, label: str, person_id: str, source_id: str | None = None
    ) -> None:
        data = self._load()
        record = {"note_id": note_id, "label": label, "person_id": person_id}
        if source_id:
            record["source_id"] = source_id
        rejections = data.setdefault("rejections", [])
        if record not in rejections:
            rejections.append(record)
            self._save(data)

    def rejected(
        self, note_id: str, label: str, person_id: str, source_id: str | None = None
    ) -> bool:
        return any(
            (r["note_id"], r["label"], r["person_id"]) == (note_id, label, person_id)
            and _same_recording(r, source_id)
            for r in self._load().get("rejections", [])
        )

    @staticmethod
    def _summary(person: dict[str, Any]) -> dict[str, Any]:
        return {
            "signatures": [
                {key: entry[key] for key in ("note_id", "created_at", "speech_seconds")}
                for entry in person.get("signatures", [])
            ],
        }

    def summary(self, person_id: str) -> dict[str, Any]:
        result = self._summary(self._load()["people"].get(person_id, {}))
        result["assignments"] = self.assignments("person:" + person_id)
        return result

    def summaries(self) -> dict[str, dict[str, Any]]:
        return {pid: self._summary(person) for pid, person in self._load()["people"].items()}

    @staticmethod
    def _clear(data: dict[str, Any], person_id: str) -> None:
        if person_id in data["people"]:
            data["people"][person_id]["signatures"] = []
        data["rejections"] = [r for r in data.get("rejections", []) if r["person_id"] != person_id]

    def delete(self, person_id: str) -> None:
        data = self._load()
        assignments = [
            a for a in data.get("assignments", []) if a["voice_id"] == "person:" + person_id
        ]
        entity_store = getattr(self.context.store, "entities", None)
        person = entity_store.load().entities.get(person_id) if entity_store else None
        self._clear_pending(assignments, person.name if person else "")
        self._clear(data, person_id)
        data["deferred"] = [
            item
            for item in data.get("deferred", [])
            if item.get("assignment", {}).get("voice_id") != "person:" + person_id
        ]
        data["assignments"] = [
            a for a in data.get("assignments", []) if a["voice_id"] != "person:" + person_id
        ]
        self._save(data)

    def defer_relabel(self, assignment: dict[str, Any], name: str) -> None:
        """Remember a speaker rename for a note that cannot be rewritten yet."""
        data = self._load()
        key = (assignment["note_id"], assignment["label"], assignment.get("source_id"))
        kept = [d for d in data.get("deferred", []) if self._deferred_key(d) != key]
        kept.append(
            {
                "note_id": assignment["note_id"],
                "label": assignment["label"],
                "assignment": dict(assignment),
                "name": name,
            }
        )
        data["deferred"] = kept
        self._save(data)

    @staticmethod
    def _deferred_key(item: dict[str, Any]) -> tuple[str, str, str | None]:
        return item["note_id"], item["label"], item.get("assignment", {}).get("source_id")

    def deferred_relabels(self) -> list[dict[str, Any]]:
        return list(self._load().get("deferred", []))

    def clear_deferred(self, note_id: str, label: str, source_id: str | None = None) -> None:
        data = self._load()
        data["deferred"] = [
            d
            for d in data.get("deferred", [])
            if self._deferred_key(d) != (note_id, label, source_id)
        ]
        self._save(data)

    def purge(self, person_ids: set[str], note_ids: set[str]) -> None:
        data = self._load()
        data["deferred"] = [
            d
            for d in data.get("deferred", [])
            if d["note_id"] not in note_ids
            and d.get("assignment", {}).get("voice_id") not in {"person:" + p for p in person_ids}
        ]
        entity_store = getattr(self.context.store, "entities", None)
        people = entity_store.load().entities if entity_store and person_ids else {}
        for person_id in person_ids:
            self._clear_pending(
                [a for a in data.get("assignments", []) if a["voice_id"] == "person:" + person_id],
                people[person_id].name if person_id in people else "",
            )
            data["people"].pop(person_id, None)
        for person in data["people"].values():
            person["signatures"] = [
                signature
                for signature in person.get("signatures", [])
                if signature["note_id"] not in note_ids
            ]
        for voice in data.get("unknowns", {}).values():
            voice["signatures"] = [
                signature
                for signature in voice.get("signatures", [])
                if signature["note_id"] not in note_ids
            ]
        data["assignments"] = [
            a
            for a in data.get("assignments", [])
            if a["note_id"] not in note_ids
            and a["voice_id"] not in {"person:" + person_id for person_id in person_ids}
        ]
        for identity, voice in list(data.get("unknowns", {}).items()):
            if not voice["signatures"] and not any(
                a["voice_id"] == identity for a in data["assignments"]
            ):
                del data["unknowns"][identity]
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
        source_id: str | None = None,
    ) -> None:
        data = self._load()
        if not data.get("recognition_enabled", True):
            return
        person = data["people"].setdefault(person_id, {"signatures": []})
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
                s["note_id"] == note_id
                and s["model"] == model
                and s["version"] == version
                and _same_recording(s, source_id)
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
                    **({"source_id": source_id} if source_id else {}),
                }
            )
        if signatures:
            self._save(data)

    def match(
        self, embeddings: list[dict[str, Any]], *, strong: float = 0.60, maybe: float = 0.45
    ) -> list[dict[str, Any]]:
        data = self._load()
        if not data.get("recognition_enabled", True):
            return []
        saved = [
            dict(signature, person_id=person_id)
            for person_id, person in data["people"].items()
            for signature in person.get("signatures", [])
        ]
        return matches(embeddings, saved, strong=strong, maybe=maybe)
