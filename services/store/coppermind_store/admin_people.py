"""People, companies and saved voices, as Admin shows and corrects them.

What Coppermind knows about someone is listed where it can be corrected. Voices
are kept as signatures only, never as audio: each can be seen, named and
removed, one person can be marked "never save", and one switch turns the whole
of voice recognition off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coppermind.entities import EntityChange
from coppermind.store_protocol import StoreError
from coppermind_store.ai_services import Refused
from coppermind_store.entities import active_facts
from coppermind_store.voice_store import VoiceStore

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

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
    return "_".join(wanted.casefold().split())


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
            enabled = voice.recognition_enabled()
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
        return {
            "people": people,
            "companies": companies,
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
                    "predicate": fact.predicate,
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
            predicate=fact["predicate"],
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

    async def voice_recognition(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The one switch for all voice recognition."""
        enabled = bool(payload.get("enabled"))
        async with self.context.lock:
            self._voice().set_recognition_enabled(enabled)
        return {"enabled": enabled}
