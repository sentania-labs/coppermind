"""Notes-native dossiers: durable decisions first, projections and SQL second.

One atomic catalog keeps multi-subject identity decisions crash safe. SQL and
Markdown below the delimiter are disposable mirrors; personal writing is not.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from coppermind import frontmatter as fm
from coppermind.atomicio import (
    atomic_write_text,
    commit_staged,
    create_exclusive_bytes,
    stage_bytes,
)
from coppermind.db.models import EntityRecord, Note, SearchDocument
from coppermind.entities import (
    Entity,
    EntityChange,
    EntityInput,
    EntityLedger,
    EntityQuery,
    Evidence,
    Fact,
)
from coppermind.ids import new_id
from coppermind.naming import sanitize_stem
from coppermind.store_protocol import (
    NoteDocument,
    NotFound,
    ValidationFailed,
    VersionConflict,
)
from coppermind_store.fs import content_hash

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

DELIMITER = "<!-- coppermind:dossier -->\n"
FACT_LINE = re.compile(r"^- (.*?) <!-- fact:([A-Za-z0-9_-]+) -->$", re.MULTILINE)
FOLDERS = ("People", "Companies")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def email(value: str) -> str | None:
    local, separator, domain = value.strip().rpartition("@")
    if not separator or not local or not domain or any(c.isspace() for c in value):
        return None
    if any(c in value for c in "<>[]\0"):
        return None
    return local + "@" + domain.lower()


def safe_path(root: Path, relative: str) -> Path:
    from coppermind_store.purge import resolve

    return resolve(root, relative)


def public(entity: Entity) -> dict[str, Any]:
    return entity.model_dump(mode="json", exclude={"generated", "pending"})


def label(value: str) -> str:
    return html.escape(value.replace("\n", " ").replace("\r", " "), quote=False)


def header(entity: Entity) -> str:
    return (
        fm.compose(
            {"entity_id": entity.id, "type": entity.kind, "aliases": entity.aliases},
            f"# {label(entity.name)}\n\n",
        )
        + DELIMITER
    )


TITLE = re.compile(r"# [^\n]*\n\n?")
GENERATED_KEYS = {"entity_id", "type", "aliases"}


def owner_wrote(head: str) -> bool:
    fields, body = fm.parse(head)
    title = TITLE.match(body)
    rest = body[title.end() :] if title else body
    return bool(set(fields) - GENERATED_KEYS) or bool(rest.strip())


def regenerate(entity: Entity, head: str) -> str:
    fields, body = fm.parse(head)
    title = TITLE.match(body)
    wanted = f"# {label(entity.name)}\n\n"
    if (
        fields.get("aliases") == entity.aliases
        and fields.get("type") == entity.kind
        and (title is None or title.group(0).strip() == wanted.strip())
    ):
        return head
    patched = fm.patch(head, {"type": entity.kind, "aliases": entity.aliases})
    _, body = fm.split(patched)
    front = patched[: len(patched) - len(body)]
    return front + (wanted + body[title.end() :] if title else body)


def active_facts(entity: Entity, ledger: EntityLedger) -> list[Fact]:
    return [
        f
        for f in entity.facts
        if f.state == "confirmed"
        and (
            f.target is None
            or (f.target in ledger.entities and not ledger.entities[f.target].retired_at)
        )
    ]


def render(entity: Entity, ledger: EntityLedger, *, search: bool = False) -> str:
    if entity.retired_at:
        return "\nRetired dossier. Restore through the entities API to use it again.\n"
    if entity.redirect:
        target = ledger.entities.get(entity.redirect)
        return (
            f"\nSee [[{target.path.removesuffix('.md')}]].\n" if target else "\nRetired identity.\n"
        )
    lines = ["", f"## {label(entity.name)}", ""]
    if entity.aliases:
        lines.extend(["Aliases: " + ", ".join(label(a) for a in entity.aliases), ""])
    if entity.emails:
        lines.extend(["Email: " + ", ".join(label(a) for a in entity.emails), ""])
    facts = active_facts(entity, ledger)
    groups = (
        ("Facts", [f for f in facts if not f.target and f.predicate != "interest"]),
        ("Relationships", [f for f in facts if f.target]),
        ("Interests", [f for f in facts if not f.target and f.predicate == "interest"]),
    )
    for heading, members in groups:
        lines += [f"## {heading}", ""]
        values: dict[str, set[str]] = {}
        for fact in members:
            values.setdefault(fact.predicate, set()).add(fact.value)
        for fact in members:
            text = f"{label(fact.predicate)}: {label(fact.value)}"
            lines.append(f"- {text} <!-- fact:{fact.id} -->")
            if fact.target:
                lines.append(f"  - [[{ledger.entities[fact.target].path.removesuffix('.md')}]]")
            if len(values[fact.predicate]) > 1 and fact.predicate not in {
                "interest",
                "mentioned",
                "note",
            }:
                lines.append("  - Other recorded values are shown with their evidence.")
            if not search:
                ev = fact.evidence
                lines.append(f"  - Evidence: [[{ev.path.removesuffix('.md')}]] {label(ev.date)}")
                if ev.quote:
                    lines.append(f"  - {label(ev.quote)}")
        lines.append("")
    if not search:
        lines += ["## Supporting notes", ""]
        paths = sorted({f.evidence.path for f in facts if f.evidence.note_id})
        lines += [f"- [[{p.removesuffix('.md')}]]" for p in paths]
        lines += ["", "Edit a fact above to correct it; delete its line to withdraw it.", ""]
    return "\n".join(lines)


class EntityService:
    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self.root = store.control.store.state_dir / "entities"
        self.lock = asyncio.Lock()

    def load(self) -> EntityLedger:
        try:
            return EntityLedger.model_validate_json((self.root / "catalog.json").read_bytes())
        except FileNotFoundError:
            return EntityLedger()

    def save(self, ledger: EntityLedger) -> None:
        ledger.revision += 1
        atomic_write_text(self.root / "catalog.json", ledger.model_dump_json(indent=2) + "\n")

    def receipt(self, ledger: EntityLedger, entity: Entity, action: str, **details: Any) -> None:
        entity.version += 1
        ledger.receipts.append(
            {
                "entity_id": entity.id,
                "action": action,
                "version": entity.version,
                "at": datetime.now(UTC).isoformat(),
                **details,
            }
        )

    def make(
        self,
        ledger: EntityLedger,
        request: EntityInput,
        *,
        automatic: bool = False,
        identity: str | None = None,
    ) -> Entity:
        addresses = [email(v) for v in request.emails]
        if any(v is None for v in addresses):
            raise ValidationFailed(["emails must be exact email addresses"])
        emails = sorted({v for v in addresses if v is not None})
        if any(set(emails) & set(e.emails) for e in ledger.entities.values() if not e.redirect):
            raise ValidationFailed(["email already belongs to a dossier; use an identity preview"])
        identity = identity or new_id()
        folder = "People" if request.kind == "person" else "Companies"
        # The suffix distinguishes same-name people and keeps paths stable after rename.
        stem = sanitize_stem(request.name)[:90]
        while len(stem.encode("utf-8")) > 220:
            stem = stem[:-1]
        path = f"{folder}/{stem} {identity}.md"
        entity = Entity(
            **request.model_dump(exclude={"emails"}),
            emails=emails,
            id=identity,
            path=path,
            automatic=automatic,
        )
        ledger.entities[identity] = entity
        return entity

    async def notes(self) -> dict[str, NoteDocument]:
        async with self.store.session_factory() as session:
            identities = list(
                (
                    await session.scalars(
                        sa.select(Note.id).where(Note.reviewed.is_(True), Note.state == "ok")
                    )
                ).all()
            )
        notes = {}
        for identity in identities:
            try:
                note = await self.store.get_note(identity)
            except (NotFound, ValidationFailed):
                continue
            if note.frontmatter.get(self.store.control.schema().role("reviewed_key")) is True:
                notes[identity] = note
        return notes

    def correction(
        self,
        entity: Entity,
        predicate: str,
        value: str,
        raw: bytes,
        previous: Fact | None = None,
        target: str | None = None,
    ) -> Fact:
        fact = Fact(
            id=new_id(),
            predicate=predicate,
            value=value,
            target=target,
            state="confirmed",
            evidence=Evidence(note_hash=content_hash(raw), path=entity.path, quote=value),
            origin="correction",
            supersedes=previous.id if previous else None,
            created_at=datetime.now(UTC),
        )
        if previous:
            previous.state = "withdrawn"
        entity.facts.append(fact)
        return fact

    def observe(self, ledger: EntityLedger, entity: Entity) -> tuple[bytes | None, str]:
        path = safe_path(self.store.notes_root, entity.path)
        if not path.exists():
            if entity.generated is not None and not entity.retired_at:
                entity.retired_at = datetime.now(UTC)
                self.receipt(ledger, entity, "page deleted")
            return None, header(entity)
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if "\0" in text:
            raise ValueError("dossier contains NUL characters")
        fields, _ = fm.parse(text)
        if fields.get("entity_id") != entity.id or text.count(DELIMITER) != 1:
            raise ValueError(
                "Dossier identity or delimiter changed; restore them to resume projection"
            )
        head, block = text.split(DELIMITER)
        entity.personal = owner_wrote(head)
        prefix = regenerate(entity, head) + DELIMITER
        if block == entity.pending:
            entity.generated, entity.pending = block, None
        if entity.generated is not None and block != entity.generated:
            old = {identity: text for text, identity in FACT_LINE.findall(entity.generated)}
            changed = {identity: text for text, identity in FACT_LINE.findall(block)}
            for fact in list(entity.facts):
                if fact.id not in old or fact.state != "confirmed":
                    continue
                line = changed.get(fact.id)
                if line == old[fact.id]:
                    continue
                if line is None:
                    fact.state = "withdrawn"
                else:
                    predicate, sep, value = html.unescape(line).partition(": ")
                    self.correction(
                        entity,
                        predicate if sep else "note",
                        value if sep else predicate,
                        raw,
                        fact,
                        fact.target,
                    )
            # Changes outside recognized fact lines are also the owner's writing.
            # Record the edited lines, without turning generated evidence into claims.
            old_lines = set(entity.generated.splitlines())
            additions = [
                line
                for line in block.splitlines()
                if line.strip() and line not in old_lines and not FACT_LINE.fullmatch(line)
            ]
            if additions:
                self.correction(entity, "note", "\n".join(additions), raw)
            entity.generated, entity.pending = block, None
            self.receipt(ledger, entity, "page correction", note_hash=content_hash(raw))
        return raw, prefix

    def project(self, ledger: EntityLedger, entity: Entity) -> None:
        try:
            raw, prefix = self.observe(ledger, entity)
            if entity.retired_at and raw is None:
                return
            block = render(entity, ledger)
            wanted = (prefix + block).encode()
            if raw == wanted:
                entity.problem = None
                return
            entity.pending = block
            self.save(ledger)  # Persist corrections and the expected replacement before writing.
            path = safe_path(self.store.notes_root, entity.path)
            if raw is None:
                create_exclusive_bytes(path, wanted)
            else:
                staged = stage_bytes(path, wanted)
                try:
                    if path.read_bytes() != raw:
                        return  # Next pass imports the device edit before writing again.
                    commit_staged(staged, path)
                finally:
                    staged.unlink(missing_ok=True)
            entity.generated, entity.pending, entity.problem = block, None, None
        except (OSError, ValueError, UnicodeError, fm.FrontmatterError):
            entity.problem = (
                "Dossier page could not be projected; check its identity, delimiter and path"
            )

    def contribute(self, ledger: EntityLedger, notes: dict[str, NoteDocument]) -> None:
        assignment = self.store.context.config().assignments["enrichment"]
        fields = {"attendees", assignment.attendees_field, assignment.contacts_field} - {"", None}
        by_email = {
            address: entity
            for entity in ledger.entities.values()
            if not entity.redirect
            for address in entity.emails
        }
        by_path = {e.path.removesuffix(".md"): e for e in ledger.entities.values()}
        for note in notes.values():
            mentions: dict[str, Entity] = {}
            for key, values in note.frontmatter.items():
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, str):
                        continue
                    entity = None
                    if value.startswith("[[") and value.endswith("]]"):
                        entity = by_path.get(value[2:-2].split("|", 1)[0].removesuffix(".md"))
                    elif key in fields and (address := email(value)):
                        entity = by_email.get(address)
                        if entity is None and digest(address) not in ledger.suppressed:
                            entity = self.make(
                                ledger,
                                EntityInput(kind="person", name=address, emails=[address]),
                                automatic=True,
                            )
                            by_email[address] = entity
                    while entity and entity.redirect:
                        entity = ledger.entities.get(entity.redirect)
                    if entity and not entity.retired_at:
                        mentions[entity.id] = entity
            for entity in mentions.values():
                identity = digest([note.id, note.content_hash, entity.id, "metadata-v1"])
                if any(f.id == identity for f in entity.facts):
                    continue
                entity.facts.append(
                    Fact(
                        id=identity,
                        predicate="mentioned",
                        value=note.title,
                        state="confirmed",
                        origin="metadata",
                        created_at=datetime.now(UTC),
                        evidence=Evidence(
                            note_id=note.id,
                            note_hash=note.content_hash,
                            path=note.path,
                            date=str(note.frontmatter.get("date", "")),
                            extraction_version="metadata-v1",
                        ),
                    )
                )
                entity.version += 1
        for entity in ledger.entities.values():
            for fact in entity.facts:
                if not fact.evidence.note_id or fact.state != "confirmed":
                    continue
                supporting = notes.get(fact.evidence.note_id)
                if supporting is None or supporting.content_hash != fact.evidence.note_hash:
                    fact.state = "withdrawn"
                    entity.version += 1
                elif fact.evidence.path != supporting.path:
                    fact.evidence.path = supporting.path

    async def migrate(self, ledger: EntityLedger) -> None:
        """Keep a stable mapping from the old email view and explicit contact corrections."""
        migrated = {r.get("legacy") for r in ledger.receipts if r.get("legacy")}
        by_email = {
            address: e for e in ledger.entities.values() if not e.redirect for address in e.emails
        }
        for legacy, record in self.store.context.read("dossiers", {}).items():
            if legacy in migrated:
                continue
            entity = by_email.get(record.get("address"))
            if entity:
                ledger.receipts.append({"legacy": legacy, "entity_id": entity.id})
        corrections = self.store.context.read("contact_corrections", {})
        if not corrections:
            return
        contacts = (await asyncio.to_thread(self.store.context.archive.catalog)).contacts()
        for contact_id, correction in corrections.items():
            key = "contact:" + contact_id + ":" + digest(correction)
            if key in migrated:
                continue
            addresses = await asyncio.to_thread(contacts.addresses, contact_id)
            matched = {by_email[a].id for a in addresses if a in by_email}
            if len(matched) != 1:
                continue  # A name or an ambiguous alias never becomes an identity guess.
            entity = ledger.entities[matched.pop()]
            if correction.get("name"):
                entity.name = correction["name"]
            target_id = correction.get("same_as")
            if target_id:
                target_addresses = await asyncio.to_thread(contacts.addresses, target_id)
                targets = {by_email[a].id for a in target_addresses if a in by_email}
                if len(targets) == 1:
                    target = ledger.entities[targets.pop()]
                    if target.id != entity.id:
                        target.emails = sorted(set(target.emails + entity.emails))
                        target.aliases = sorted(
                            set(target.aliases + entity.aliases + [entity.name])
                        )
                        target.facts.extend(entity.facts)
                        entity.redirect = target.id
                        target.version += 1
            self.receipt(ledger, entity, "legacy contact correction", legacy=key)

    async def follow(self, note: NoteDocument, before_hash: str) -> None:
        """A store-owned filing rewrite carries unchanged quoted claims to its new bytes."""
        async with self.store.maintenance.shared(), self.lock:
            if self.purge_pending():
                return
            ledger = self.load()
            changed = False
            for entity in ledger.entities.values():
                for fact in entity.facts:
                    ev = fact.evidence
                    if (
                        ev.note_id == note.id
                        and ev.note_hash == before_hash
                        and (not ev.quote or ev.quote in note.body)
                    ):
                        ev.note_hash, ev.path = note.content_hash, note.path
                        entity.version += 1
                        changed = True
            if changed:
                self.save(ledger)

    def purge_pending(self) -> bool:
        from coppermind_store.purge import records

        return any(r["state"] in {"applying", "failed"} for r in records(self.store))

    async def refresh_locked(self, ledger: EntityLedger) -> None:
        if self.purge_pending():
            raise ValidationFailed(["Finish or abandon the interrupted retention purge first"])
        before = ledger.model_dump_json()
        # Import device corrections before adding/retracting metadata contributions.
        for entity in list(ledger.entities.values()):
            try:
                self.observe(ledger, entity)
            except (OSError, ValueError, UnicodeError, fm.FrontmatterError):
                entity.problem = "Dossier page identity, delimiter or path needs repair"
        self.contribute(ledger, await self.notes())
        await self.migrate(ledger)
        for entity in ledger.entities.values():
            self.project(ledger, entity)
        if ledger.model_dump_json() != before:
            self.save(ledger)
        await self.mirror(ledger)

    async def refresh(self) -> None:
        async with self.store.maintenance.shared(), self.lock:
            if self.purge_pending():
                return
            await self.refresh_locked(self.load())

    def personal_text(self, entity: Entity) -> str:
        try:
            text = safe_path(self.store.notes_root, entity.path).read_text()
            fields, _ = fm.parse(text)
            if fields.get("entity_id") != entity.id or text.count(DELIMITER) != 1:
                return ""
            _, body = fm.parse(text.split(DELIMITER, 1)[0])
            return body
        except (OSError, ValueError, UnicodeError, fm.FrontmatterError):
            return ""

    async def mirror(self, ledger: EntityLedger) -> None:
        async with self.store.session_factory() as session, session.begin():
            await session.execute(
                sa.delete(EntityRecord).where(EntityRecord.id.not_in(ledger.entities))
            )
            await session.execute(
                sa.delete(SearchDocument).where(
                    SearchDocument.kind == "entity", SearchDocument.ref_id.not_in(ledger.entities)
                )
            )
            held_records = {
                row.id: (row.kind, row.name, row.version, row.active, row.record)
                for row in (await session.execute(sa.select(EntityRecord))).scalars()
            }
            held_documents = {
                row.ref_id: (row.path, row.title, row.body, row.version)
                for row in (
                    await session.execute(
                        sa.select(SearchDocument).where(SearchDocument.kind == "entity")
                    )
                ).scalars()
            }
            for entity in ledger.entities.values():
                record = public(entity)
                inactive = bool(entity.retired_at or entity.redirect)
                body = (
                    None
                    if inactive
                    else self.personal_text(entity) + render(entity, ledger, search=True)
                )
                values = dict(
                    id=entity.id,
                    kind=entity.kind,
                    name=entity.name,
                    version=entity.version,
                    active=not inactive,
                    record=record,
                )
                if held_records.get(entity.id) != (
                    entity.kind,
                    entity.name,
                    entity.version,
                    not inactive,
                    record,
                ):
                    stmt = insert(EntityRecord).values(**values)
                    await session.execute(
                        stmt.on_conflict_do_update(index_elements=[EntityRecord.id], set_=values)
                    )
                if body is None:
                    await session.execute(
                        sa.delete(SearchDocument).where(
                            SearchDocument.kind == "entity", SearchDocument.ref_id == entity.id
                        )
                    )
                    continue
                if held_documents.get(entity.id) == (
                    entity.path,
                    entity.name,
                    body,
                    str(entity.version),
                ):
                    continue
                values = dict(
                    kind="entity",
                    ref_id=entity.id,
                    path=entity.path,
                    title=entity.name,
                    body=body,
                    version=str(entity.version),
                    indexed_at=datetime.now(UTC),
                )
                stmt = insert(SearchDocument).values(**values)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[SearchDocument.kind, SearchDocument.ref_id], set_=values
                    )
                )

    async def list(self, query: EntityQuery) -> dict[str, Any]:
        async with self.store.maintenance.shared(), self.lock:
            if self.purge_pending():
                raise ValidationFailed(["Finish or abandon the interrupted retention purge first"])
            ledger = self.load()
            signature = digest(query.model_dump(exclude={"cursor", "limit"}))[:16]
            after = ""
            if query.cursor:
                prefix, _, after = query.cursor.partition(":")
                if prefix != signature:
                    raise ValidationFailed(["cursor belongs to different entity filters"])
            items = []
            for entity in sorted(ledger.entities.values(), key=lambda e: e.id):
                if (
                    entity.id <= after
                    or bool(entity.retired_at or entity.redirect) != query.retired
                ):
                    continue
                if query.kind and entity.kind != query.kind:
                    continue
                if query.alias and query.alias.casefold() not in {
                    a.casefold() for a in entity.aliases
                }:
                    continue
                haystack = " ".join([entity.name, *entity.aliases, *entity.emails]).casefold()
                if query.q.casefold() not in haystack:
                    continue
                if (query.relationship or query.target) and not any(
                    (not query.relationship or f.predicate == query.relationship)
                    and (not query.target or f.target == query.target)
                    for f in active_facts(entity, ledger)
                ):
                    continue
                items.append(public(entity))
            page = items[: query.limit]
            cursor = signature + ":" + page[-1]["id"] if len(items) > query.limit else None
            return {"items": page, "next_cursor": cursor}

    async def get(self, identity: str) -> dict[str, Any]:
        async with self.store.maintenance.shared(), self.lock:
            if self.purge_pending():
                raise ValidationFailed(["Finish or abandon the interrupted retention purge first"])
            ledger = self.load()
            if identity not in ledger.entities:
                raise NotFound(identity)
            return public(ledger.entities[identity]) | {
                "personal_text": self.personal_text(ledger.entities[identity])
            }

    async def create(self, request: EntityInput) -> dict[str, Any]:
        async with self.store.maintenance.shared(), self.lock:
            if self.purge_pending():
                raise ValidationFailed(["Finish or abandon the interrupted retention purge first"])
            ledger = self.load()
            entity = self.make(ledger, request)
            self.receipt(ledger, entity, "created")
            self.save(ledger)
            self.project(ledger, entity)
            self.save(ledger)
            await self.mirror(ledger)
            return public(entity)

    async def change(self, identity: str, change: EntityChange) -> dict[str, Any]:
        async with self.store.maintenance.shared(), self.lock:
            ledger = self.load()
            await self.refresh_locked(ledger)
            if identity not in ledger.entities:
                raise NotFound(identity)
            entity = ledger.entities[identity]
            if entity.version != change.version:
                raise VersionConflict(str(entity.version))
            if (entity.retired_at or entity.redirect) and change.action != "restore":
                raise ValidationFailed(["restore this dossier before changing it"])
            if change.action in {"merge", "merge-preview", "split", "split-preview"}:
                return await self.identity(ledger, entity, change)
            fact = next((f for f in entity.facts if f.id == change.fact_id), None)
            if change.action in {"correct", "withdraw"} and fact is None:
                raise ValidationFailed(["fact_id must name a fact on this dossier"])
            if change.target and (
                change.target not in ledger.entities
                or ledger.entities[change.target].retired_at
                or ledger.entities[change.target].redirect
            ):
                raise ValidationFailed(["target must name an active dossier"])
            if change.action == "rename":
                entity.name = EntityInput.single_line(change.name or "")
            elif change.action == "aliases":
                candidate = EntityInput(
                    kind=entity.kind,
                    name=entity.name,
                    aliases=entity.aliases if change.aliases is None else change.aliases,
                    emails=entity.emails if change.emails is None else change.emails,
                )
                addresses = [email(a) for a in candidate.emails]
                if None in addresses or any(
                    set(addresses) & set(e.emails)
                    for e in ledger.entities.values()
                    if e.id != identity and not e.redirect
                ):
                    raise ValidationFailed(
                        ["email is invalid or already belongs to another dossier"]
                    )
                entity.aliases, entity.emails = candidate.aliases, [a for a in addresses if a]
            elif change.action == "claim":
                if not change.note_id or not change.quote.strip():
                    raise ValidationFailed(
                        ["claims need a reviewed note and an exact prose quotation"]
                    )
                note = await self.store.get_note(change.note_id)
                if (
                    note.frontmatter.get(self.store.control.schema().role("reviewed_key"))
                    is not True
                    or change.quote not in note.body
                ):
                    raise ValidationFailed(["claim evidence must quote the current reviewed note"])
                entity.facts.append(
                    Fact(
                        id=new_id(),
                        predicate=change.predicate,
                        value=change.value,
                        target=change.target,
                        state="confirmed",
                        origin="prose",
                        created_at=datetime.now(UTC),
                        evidence=Evidence(
                            note_id=note.id,
                            path=note.path,
                            note_hash=note.content_hash,
                            quote=change.quote,
                            date=str(note.frontmatter.get("date", "")),
                            extraction_version="agent-v1",
                        ),
                    )
                )
            elif change.action == "correct":
                self.correction(
                    entity,
                    change.predicate,
                    change.value,
                    change.model_dump_json().encode(),
                    fact,
                    change.target,
                )
            elif change.action == "withdraw":
                assert fact is not None
                fact.state = "withdrawn"
            elif change.action == "retire":
                entity.retired_at = datetime.now(UTC)
            elif change.action == "restore":
                if entity.redirect:
                    raise ValidationFailed(
                        ["merged IDs remain redirects; preview a split to separate facts"]
                    )
                entity.retired_at = None
                # Explicit restore permits recreation of a deleted page.
                if not safe_path(self.store.notes_root, entity.path).exists():
                    entity.generated, entity.pending = None, None
            self.receipt(ledger, entity, change.action, fact_id=change.fact_id)
            self.save(ledger)
            self.project(ledger, entity)
            self.save(ledger)
            await self.mirror(ledger)
            return public(entity)

    async def identity(
        self, ledger: EntityLedger, entity: Entity, change: EntityChange
    ) -> dict[str, Any]:
        splitting = change.action.startswith("split")
        target = ledger.entities.get(change.target or "")
        if not splitting and (
            target is None
            or target.id == entity.id
            or target.retired_at
            or target.redirect
            or target.kind != entity.kind
        ):
            raise ValidationFailed(["choose another active dossier of the same kind"])
        if splitting and (
            not change.name
            or not change.fact_ids
            or not set(change.fact_ids) <= {f.id for f in entity.facts}
        ):
            raise ValidationFailed(["split requires a new name and facts from this dossier"])
        selected = [f for f in entity.facts if not splitting or f.id in change.fact_ids]
        preview = {
            "subject": entity.id,
            "target": target.id if target else None,
            "name": change.name,
            "emails": change.emails,
            "facts": [f.id for f in selected],
            "notes": sorted({f.evidence.note_id for f in selected if f.evidence.note_id}),
            "revision": ledger.revision,
        }
        token = digest(preview)
        if change.action.endswith("-preview"):
            return {"preview": token, "affected": preview, "version": entity.version}
        if change.preview != token:
            raise ValidationFailed(["identity preview changed; preview again before applying"])
        if splitting:
            target = self.make(ledger, EntityInput(kind=entity.kind, name=change.name or ""))
            target.facts.extend(f.model_copy(deep=True) for f in selected)
            for fact in selected:
                fact.state = "withdrawn"
            if change.emails:
                if not set(change.emails) <= set(entity.emails):
                    raise ValidationFailed(["split emails must belong to the original dossier"])
                target.emails = sorted(set(change.emails))
                entity.emails = [a for a in entity.emails if a not in change.emails]
        else:
            assert target is not None
            target.facts.extend(f for f in selected if f.id not in {v.id for v in target.facts})
            target.aliases = sorted(set(target.aliases + entity.aliases + [entity.name]))
            target.emails = sorted(set(target.emails + entity.emails))
            entity.redirect = target.id
            for other in ledger.entities.values():
                for fact in other.facts:
                    if fact.target == entity.id:
                        fact.target = target.id
                        other.version += 1
        self.receipt(ledger, entity, change.action, target=target.id, preview=token)
        self.receipt(ledger, target, change.action, source=entity.id)
        self.save(ledger)
        for item in ledger.entities.values():
            self.project(ledger, item)
        self.save(ledger)
        await self.mirror(ledger)
        return public(target)

    def purge_items(self, cutoff: datetime, note_ids: set[str]) -> builtins.list[dict[str, Any]]:
        """Include retired subjects and unsupported automatic subjects in retention preview."""
        from coppermind.purge import PurgeCandidate
        from coppermind_store.purge import _file

        items = []
        for entity in self.load().entities.values():
            retired = entity.retired_at is not None and entity.retired_at <= cutoff
            orphan = (
                entity.automatic
                and not entity.personal
                and not entity.redirect
                and bool(entity.facts)
                and all(
                    f.origin != "correction" and f.evidence.note_id in note_ids
                    for f in entity.facts
                )
            )
            if not retired and not orphan:
                continue
            path = safe_path(self.store.notes_root, entity.path)
            if orphan and not retired and path.exists():
                text = path.read_text()
                if text.count(DELIMITER) != 1:
                    continue
                head, block = text.split(DELIMITER)
                try:
                    wrote = owner_wrote(head)
                except (ValueError, fm.FrontmatterError):
                    continue
                if wrote or block not in {entity.generated, entity.pending}:
                    continue  # A fresh device edit is personal work, even before reconciliation.
            files = [_file(self.store, "notes", path)] if path.exists() else []
            candidate = PurgeCandidate(
                kind="entity",
                id=entity.id,
                title=entity.name,
                path=entity.path,
                origins=["Dossiers"],
                providers=["Local notes"],
                deleted_at=entity.retired_at,
            )
            items.append(
                {
                    "candidate": candidate.model_dump(mode="json"),
                    "row": digest(entity.model_dump(mode="json")),
                    "files": files,
                    "directories": [],
                }
            )
        return items

    def purge(self, note_ids: set[str], entity_ids: set[str], token: str) -> None:
        """Idempotent scrubbing while the caller holds the exclusive maintenance gate."""
        ledger = self.load()
        if any(r.get("purge") == token for r in ledger.receipts):
            return
        for identity in entity_ids:
            entity = ledger.entities.pop(identity, None)
            if entity:
                ledger.suppressed.extend(digest(address) for address in entity.emails)
        for entity in ledger.entities.values():
            entity.facts = [
                f
                for f in entity.facts
                if f.evidence.note_id not in note_ids and f.target not in entity_ids
            ]
            if entity.redirect in entity_ids:
                entity.retired_at = datetime.now(UTC)
                entity.redirect = None
            entity.version += 1
        ledger.receipts = [
            r
            for r in ledger.receipts
            if r.get("entity_id") not in entity_ids
            and r.get("target") not in entity_ids
            and r.get("source") not in entity_ids
        ]
        ledger.suppressed = sorted(set(ledger.suppressed))
        # Remove the legacy mirrors and per-note context records too. Originals
        # remain until retention confirmation, not merely a filesystem absence.
        for name in ("finalized", "associations", "calendar-followups"):
            records = self.store.context.read(name, {})
            for identity in note_ids:
                records.pop(identity, None)
            self.store.context.write(name, records)
        for identity in note_ids:
            (self.store.context.root / "review-completions" / (identity + ".json")).unlink(
                missing_ok=True
            )
        for path in (self.store.context.root / "association-corrections").glob("*.json"):
            if json.loads(path.read_bytes()).get("note_id") in note_ids:
                path.unlink()
        for job in self.store.context.jobs():
            if job.note_id in note_ids:
                (self.store.context.root / "jobs" / (job.id + ".json")).unlink(missing_ok=True)
        self.store.context.write("dossiers", {})
        self.save(ledger)
        for entity in ledger.entities.values():
            self.project(ledger, entity)
            # Retired pages may be absent, but their old projection cache must not
            # retain quotations from a purged supporting note.
            if entity.retired_at:
                entity.generated, entity.pending = None, None
        ledger.receipts.append({"purge": token})
        self.save(ledger)
