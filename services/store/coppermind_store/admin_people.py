"""People, companies and saved voices, as Admin shows and corrects them.

What Coppermind knows about someone is listed where it can be corrected. Voices
are kept as signatures only, never as audio: each can be seen, named and
removed, one person can be marked "never save", and one switch turns the whole
of voice recognition off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coppermind.entities import EntityChange, EntityInput
from coppermind.store_protocol import StoreError
from coppermind_store.ai_services import Refused
from coppermind_store.entities import active_facts
from coppermind_store.voice_store import VoiceStore

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

# The three named levels, as the two numbers voice matching works with.
LEVELS = {
    "Careful": {"strong": 0.75, "maybe": 0.75},
    "Balanced": {"strong": 0.60, "maybe": 0.45},
    "Eager": {"strong": 0.50, "maybe": 0.35},
}
PREDICATES = {
    "role": "Role",
    "organization": "Company",
    "relationship": "Relationship",
    "mentioned": "Mentioned in",
    "note": "Note",
    "interest": "Interest",
    "email": "Email",
}


def label_of(predicate: str) -> str:
    return PREDICATES.get(predicate, predicate.replace("_", " ").strip().capitalize())


def predicate_of(label: str) -> str:
    wanted = label.strip()
    for predicate, name in PREDICATES.items():
        if name.casefold() == wanted.casefold():
            return predicate
    return wanted


class AdminPeople:
    def __init__(self, store: LocalStore) -> None:
        self.store = store

    @property
    def context(self) -> Any:
        return self.store.context

    def _voice(self) -> VoiceStore:
        return VoiceStore(self.context)

    def _active(self) -> tuple[Any, list[Any]]:
        ledger = self.store.entities.load()
        return ledger, [
            entity
            for entity in ledger.entities.values()
            if not entity.retired_at and not entity.redirect
        ]

    def _company_of(self, entity: Any, ledger: Any) -> str:
        for fact in active_facts(entity, ledger):
            if fact.predicate == "organization":
                target = ledger.entities.get(fact.target or "")
                return target.name if target else fact.value
        return ""

    def _notes(self, entity: Any) -> int:
        return len(
            {
                fact.evidence.note_id
                for fact in entity.facts
                if fact.state == "confirmed" and fact.evidence.note_id
            }
        )

    async def listing(self) -> dict[str, Any]:
        async with self.context.lock:
            voice = self._voice()
            saved = voice.summaries()
            never = voice.never_saved()
            unknowns = voice.unknown_summaries()
            enabled = voice.recognition_enabled()
            thresholds = self.context.read("voice-settings", LEVELS["Balanced"])
        ledger, active = self._active()
        people = []
        companies = []
        for entity in sorted(active, key=lambda item: item.name.casefold()):
            if entity.kind == "person":
                signatures = saved.get(entity.id, {}).get("signatures", [])
                people.append(
                    {
                        "id": entity.id,
                        "name": entity.name,
                        "company": self._company_of(entity, ledger),
                        "notes": self._notes(entity),
                        "samples": len(signatures),
                        "heard": max((s["created_at"] for s in signatures), default=""),
                        "never": entity.id in never,
                    }
                )
            else:
                members = [
                    other.name
                    for other in active
                    if other.kind == "person"
                    and any(
                        fact.predicate == "organization" and fact.target == entity.id
                        for fact in active_facts(other, ledger)
                    )
                ]
                companies.append(
                    {
                        "id": entity.id,
                        "name": entity.name,
                        "people": len(members),
                        "notes": self._notes(entity),
                    }
                )
        unnamed = []
        for identity, item in unknowns.items():
            appearances = sorted(
                item["assignments"], key=lambda a: a.get("created_at", ""), reverse=True
            )
            titles = await self._recording_titles({a["note_id"] for a in appearances})
            first = appearances[0] if appearances else None
            unnamed.append(
                {
                    "id": identity,
                    "name": item["name"],
                    "label": first["label"] if first else item["name"],
                    "where": titles.get(first["note_id"], "") if first else "",
                    "note_id": first["note_id"] if first else "",
                    "heard": max((s["created_at"] for s in item["signatures"]), default=""),
                    "more": max(0, len({a["note_id"] for a in appearances}) - 1),
                }
            )
        level = next(
            (
                name
                for name, values in LEVELS.items()
                if abs(values["strong"] - thresholds["strong"]) < 1e-6
                and abs(values["maybe"] - thresholds["maybe"]) < 1e-6
            ),
            "",
        )
        return {
            "people": people,
            "companies": companies,
            "unnamed": unnamed,
            "level": level,
            "thresholds": thresholds,
            "recognition": enabled,
        }

    async def _recording_titles(self, note_ids: set[str]) -> dict[str, str]:
        titles = {}
        for job in self.context.recordings.jobs():
            for key in (job.note_id, "recording:" + job.id):
                if key in note_ids:
                    titles[key] = job.title
        for note_id in note_ids - set(titles):
            try:
                titles[note_id] = (await self.store.get_note(note_id)).title
            except StoreError:
                continue
        return titles

    async def detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = str(payload["id"])
        ledger, active = self._active()
        entity = ledger.entities.get(identity)
        if entity is None or entity.retired_at or entity.redirect:
            raise ValueError("That person or company is no longer listed.")
        facts = []
        for fact in active_facts(entity, ledger):
            if fact.predicate == "mentioned":
                continue
            target = ledger.entities.get(fact.target or "")
            facts.append(
                {
                    "id": fact.id,
                    "label": label_of(fact.predicate),
                    "value": target.name if target else fact.value,
                    "by_you": fact.origin == "correction",
                    "from": fact.evidence.path.rsplit("/", 1)[-1].removesuffix(".md"),
                    "at": fact.created_at.isoformat(),
                }
            )
        for address in entity.emails:
            facts.append(
                {
                    "id": "",
                    "label": "Email",
                    "value": address,
                    "by_you": False,
                    "from": "",
                    "at": "",
                    "fixed": True,
                }
            )
        result: dict[str, Any] = {
            "id": entity.id,
            "kind": entity.kind,
            "name": entity.name,
            "path": entity.path,
            "version": entity.version,
            "notes": self._notes(entity),
            "facts": facts,
        }
        if entity.kind == "company":
            result["people"] = [
                {"id": other.id, "name": other.name}
                for other in active
                if other.kind == "person"
                and any(
                    fact.predicate == "organization" and fact.target == entity.id
                    for fact in active_facts(other, ledger)
                )
            ]
            return result
        async with self.context.lock:
            voice = self._voice()
            summary = voice.summary(entity.id)
            result["never"] = entity.id in voice.never_saved()
        titles = await self._recording_titles({s["note_id"] for s in summary["signatures"]})
        result["samples"] = [
            {
                "title": titles.get(signature["note_id"], "a recording"),
                "at": signature["created_at"],
            }
            for signature in summary["signatures"]
        ]
        return result

    async def _change(self, identity: str, **fields: Any) -> dict[str, Any]:
        ledger = self.store.entities.load()
        entity = ledger.entities.get(identity)
        if entity is None:
            raise ValueError("That person or company is no longer listed.")
        return await self.store.entities.change(
            identity, EntityChange(version=entity.version, **fields)
        )

    async def fact_correct(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = str(payload.get("value", "")).strip()
        if not value:
            raise Refused("value", "Type the fact.")
        detail = await self.detail({"id": payload["id"]})
        fact = next((f for f in detail["facts"] if f["id"] == payload.get("fact_id")), None)
        if fact is None:
            raise ValueError("That fact is no longer listed.")
        await self._change(
            str(payload["id"]),
            action="correct",
            fact_id=fact["id"],
            predicate=predicate_of(fact["label"]),
            value=value,
        )
        return {"label": fact["label"], "value": value, "was": fact["value"]}

    async def fact_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        label, value = str(payload.get("label", "")).strip(), str(payload.get("value", "")).strip()
        if not label:
            raise Refused("label", "Say what the fact is, for example Phone.")
        if not value:
            raise Refused("value", "Type the fact.")
        await self._change(
            str(payload["id"]), action="add", predicate=predicate_of(label), value=value
        )
        return {"label": label, "value": value}

    async def fact_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        detail = await self.detail({"id": payload["id"]})
        fact = next((f for f in detail["facts"] if f["id"] == payload.get("fact_id")), None)
        if fact is None:
            raise ValueError("That fact is no longer listed.")
        await self._change(str(payload["id"]), action="withdraw", fact_id=fact["id"])
        return {"label": fact["label"], "value": fact["value"]}

    async def remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        detail = await self.detail({"id": payload["id"]})
        await self._change(str(payload["id"]), action="retire")
        return {"name": detail["name"], "kind": detail["kind"]}

    # -- voices --------------------------------------------------------------------------

    async def voice_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = str(payload["id"])
        detail = await self.detail({"id": identity})
        async with self.context.lock:
            voice = self._voice()
            count = len(voice.summary(identity)["signatures"])
            if payload.get("never"):
                voice.set_never(identity, True)
            else:
                voice.delete(identity)
        return {"name": detail["name"], "samples": count, "never": bool(payload.get("never"))}

    async def voice_never(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = str(payload["id"])
        detail = await self.detail({"id": identity})
        async with self.context.lock:
            self._voice().set_never(identity, bool(payload.get("never")))
        return {"name": detail["name"], "never": bool(payload.get("never"))}

    async def voice_name(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Name an unnamed voice as someone known, or as someone new."""
        from coppermind_store.voice_relabel import combine

        voice_id = str(payload["id"])
        person_id = str(payload.get("person") or "")
        new_name = str(payload.get("new_name") or "").strip()
        if not person_id and not new_name:
            raise Refused(
                "person",
                'Choose who this voice belongs to, or pick "Someone new" and type their name, '
                "then press Save.",
            )
        if not person_id:
            ledger, active = self._active()
            known = next(
                (
                    entity
                    for entity in active
                    if entity.kind == "person" and entity.name.casefold() == new_name.casefold()
                ),
                None,
            )
            if known is None:
                created = await self.store.entities.create(
                    EntityInput(kind="person", name=new_name)
                )
                person_id = created["id"]
            else:
                person_id = known.id
        async with self.context.lock:
            unknown = self._voice().unknown_summaries().get(voice_id)
        if unknown is None:
            raise ValueError("That voice is no longer listed.")
        recordings = len({a["note_id"] for a in unknown["assignments"]})
        async with self.context.lifecycle.lock, self.context.lock:
            await combine(self.context, voice_id, "person:" + person_id)
        detail = await self.detail({"id": person_id})
        return {"name": detail["name"], "recordings": max(1, recordings)}

    async def voice_forget(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.context.lock:
            self._voice().delete_unknown(str(payload["id"]))
        return {}

    async def voice_level(self, payload: dict[str, Any]) -> dict[str, Any]:
        level = str(payload["level"])
        if level not in LEVELS:
            raise ValueError("choose Careful, Balanced or Eager")
        async with self.context.lock:
            self.context.write("voice-settings", LEVELS[level])
        return {"level": level}

    async def voice_recognition(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The one switch for all voice recognition."""
        enabled = bool(payload.get("enabled"))
        async with self.context.lock:
            self._voice().set_recognition_enabled(enabled)
        return {"enabled": enabled}

    async def clip(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Where one speaker can be heard, while the recording's audio is still kept.

        Signatures are all that is kept of a voice. A recording's audio is deleted once
        its note is reviewed, so a clip exists only for a recording still waiting.
        """
        note_id, label = str(payload.get("note_id", "")), str(payload.get("label", ""))
        for job in self.context.recordings.jobs():
            if note_id not in {job.note_id, "recording:" + job.id} or job.audio_removed:
                continue
            if not job.transcribed:
                continue
            transcript = self.context.recordings.saved_transcript(job)
            spans = [
                (segment["start"], segment["end"])
                for segment in transcript.get("segments", [])
                if segment.get("speaker") == label
                and segment.get("start") is not None
                and segment.get("end") is not None
            ]
            if not spans:
                continue
            start, end = max(spans, key=lambda span: span[1] - span[0])
            return {
                "artifact_id": job.artifact_id,
                "start": round(float(start), 1),
                "end": round(min(float(end), float(start) + 8), 1),
                "mime_type": job.mime_type,
            }
        return {}
