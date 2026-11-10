"""Notes-native dossiers: durable decisions first, projections and SQL second.

One atomic catalog keeps multi-subject identity decisions crash safe. SQL and
Markdown below the delimiter are disposable mirrors; personal writing is not.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import html
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from coppermind import frontmatter as fm
from coppermind.atomicio import (
    atomic_write_text,
    commit_staged,
    create_exclusive_bytes,
    stage_bytes,
    sync_directory,
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
    StoreError,
    ValidationFailed,
    VersionConflict,
)
from coppermind_store.fs import content_hash

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

DELIMITER = "<!-- coppermind:dossier -->\n"
MARKER = re.compile(r"(?m)^<!-- coppermind:dossier(?: id=([A-Za-z0-9_-]*))? -->\r?\n")
WIKI_TARGET = re.compile(r"!?\[\[([^\]#|\\]+)")
FACT_LINE = re.compile(r"^- (.*?) <!-- fact:([A-Za-z0-9_-]+) -->$", re.MULTILINE)
FOLDERS = ("People", "Companies")


def dossier_marker(identity: str) -> str:
    return f"<!-- coppermind:dossier id={identity} -->\n"


def dossier_parts(text: str, entity: Entity) -> tuple[str, str]:
    """Split old and new markers; recover a missing marker without dropping bytes."""
    markers = list(MARKER.finditer(text))
    if len(markers) > 1:
        raise ValueError("multiple dossier markers")
    if markers:
        found = markers[0]
        if found.group(1) and found.group(1) != entity.id:
            raise ValueError("dossier marker belongs to another entity")
        return text[: found.start()], text[found.end() :]
    if entity.generated and text.endswith(entity.generated):
        return text[: -len(entity.generated)], entity.generated
    raise ValueError("dossier marker is missing; restore it to resume")


def matches_identity(fields: dict[str, Any], entity: Entity, domains: list[str]) -> bool:
    """Exact identity of a page in a store-owned folder: email, or domains, or name with aliases."""

    def strings(key: str) -> set[str]:
        value = fields.get(key, [])
        value = [value] if isinstance(value, str) else value
        return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()

    if entity.kind == "person":
        return bool((strings("email") | strings("aliases")) & set(entity.emails))
    names = {entity.name, *entity.aliases}
    aliases = fields.get("aliases")
    if domains and strings("domains") == set(domains):
        return True
    name = fields.get("name")
    return (
        isinstance(name, str)
        and name in names
        and isinstance(aliases, list)
        and all(isinstance(a, str) for a in aliases)
        and set(aliases) <= names
    )


class NoteRef(NamedTuple):
    id: str
    title: str
    path: str
    content_hash: str
    frontmatter: dict[str, Any]


def recorded(
    entity: Entity,
    identity: str,
    note: NoteRef,
    version: str,
    predicate: str,
    value: str,
    target: str | None = None,
) -> bool:
    return any(
        f.id == identity
        or (
            f.evidence.note_id == note.id
            and f.evidence.note_hash == note.content_hash
            and f.evidence.extraction_version == version
            and f.predicate == predicate
            and f.value == value
            and f.target == target
        )
        for f in entity.facts
    )


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def email(value: str) -> str | None:
    local, separator, domain = value.strip().rpartition("@")
    if not separator or not local or not domain or any(c.isspace() for c in value):
        return None
    if len(value.strip()) > 200:
        return None
    if any(c in value for c in "<>[]\0"):
        return None
    return local + "@" + domain.lower()


def email_name(address: str) -> str:
    """Give an email-only person a readable provisional name."""
    local = address.rsplit("@", 1)[0]
    return " ".join(part.capitalize() for part in re.split(r"[._+\-]+", local) if part) or address


def rewrite_dossier_links(text: str, old: str, new: str) -> str:
    """Change exact wiki targets and preserve every other character."""
    old_path, new_path = old.removesuffix(".md"), new.removesuffix(".md")
    names = {old_path: new_path, Path(old_path).name: Path(new_path).name}
    pattern = re.compile(r"(!?\[\[)([^\]#|\\]+)([^\]]*\]\])")

    def replace(match: re.Match[str]) -> str:
        target = match.group(2)
        extension = ".md" if target.endswith(".md") else ""
        replacement = names.get(target.removesuffix(".md"))
        return (
            match.group(0)
            if replacement is None
            else match.group(1) + replacement + extension + match.group(3)
        )

    return pattern.sub(replace, text)


def safe_path(root: Path, relative: str) -> Path:
    from coppermind_store.purge import resolve

    return resolve(root, relative)


def public(entity: Entity) -> dict[str, Any]:
    return entity.model_dump(
        mode="json", exclude={"generated", "pending", "projection_fields", "page_move"}
    )


def label(value: str) -> str:
    return html.escape(value.replace("\n", " ").replace("\r", " "), quote=False)


PUBLIC_MAIL = frozenset(
    {
        "aol.com",
        "fastmail.com",
        "gmail.com",
        "gmx.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "mac.com",
        "me.com",
        "msn.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
    }
)
DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


def squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def domain_belongs(domain: str, company: Entity) -> bool:
    parts = domain.rpartition(".")[0]
    labels = {squash(parts.rpartition(".")[2]), *map(squash, re.split(r"[.-]", parts))}
    return domain not in PUBLIC_MAIL and any(
        len(name) >= 3 and name in labels for name in map(squash, [company.name, *company.aliases])
    )


def company_domains(entity: Entity, ledger: EntityLedger) -> list[str]:
    return sorted({f.value for f in active_facts(entity, ledger) if f.predicate == "domain"})


def domain_owners(ledger: EntityLedger) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for company in ledger.entities.values():
        if company.kind == "company" and not company.retired_at and not company.redirect:
            for domain in company_domains(company, ledger):
                owners.setdefault(domain, set()).add(company.id)
    return owners


def properties(
    entity: Entity, ledger: EntityLedger, owners: dict[str, set[str]] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "aliases": entity.aliases,
        "name": entity.name,
    }
    if entity.emails:
        result["email"] = entity.emails
    if entity.kind == "company":
        result["domains"] = company_domains(entity, ledger)
    if entity.kind == "person":
        linked = {
            f.target
            for f in active_facts(entity, ledger)
            if f.predicate == "organization"
            and f.target
            and ledger.entities[f.target].kind == "company"
        }
        owners = domain_owners(ledger) if owners is None else owners
        for address in entity.emails:
            matched = owners.get(address.rpartition("@")[2].lower(), set())
            if len(matched) == 1:
                linked |= matched
        result["company"] = sorted(
            f"[[{ledger.entities[identity].path.removesuffix('.md')}]]" for identity in linked
        )
    return result


def header(entity: Entity, ledger: EntityLedger, owners: dict[str, set[str]] | None = None) -> str:
    return fm.compose(
        properties(entity, ledger, owners),
        f"# {label(entity.name)}\n\n## Scott's Notes\n\n",
    ) + dossier_marker(entity.id)


TITLE = re.compile(r"# [^\n]*\n\n?")
GENERATED_KEYS = {"entity_id", "type", "aliases", "name", "email", "company", "domains"}


def owner_wrote(head: str) -> bool:
    fields, body = fm.parse(head)
    title = TITLE.match(body)
    rest = body[title.end() :] if title else body
    rest = rest.replace("## Scott's Notes\n", "", 1)
    return bool(set(fields) - GENERATED_KEYS) or bool(rest.strip())


def regenerate(entity: Entity, head: str) -> str:
    fields, body = fm.parse(head)
    title = TITLE.match(body)
    wanted = f"# {label(entity.name)}\n\n"
    if fields.get("aliases") == entity.aliases and (
        title is None or title.group(0).strip() == wanted.strip()
    ):
        return head
    patched = fm.replace_value_in_place(head, "aliases", entity.aliases) or head
    _, body = fm.split(patched)
    front = patched[: len(patched) - len(body)]
    return front + (wanted + body[title.end() :] if title else body)


def remove_legacy_identity(head: str, entity: Entity) -> str:
    fields, _ = fm.parse(head)
    block, body = fm.split(head)
    if fields.get("entity_id") == entity.id:
        block = re.sub(r"(?m)^entity_id:[^\r\n]*(?:\r?\n|$)", "", block)
    if fields.get("type") == entity.kind:
        block = re.sub(r"(?m)^type:[^\r\n]*(?:\r?\n|$)", "", block)
    # Keep all other frontmatter and personal body bytes unchanged.
    opening = "---\r\n" if head.startswith("---\r\n") else "---\n"
    closing = "---\r\n" if "\r\n" in head[: len(head) - len(body)] else "---\n"
    return opening + block + closing + body


def rename(entity: Entity, name: str) -> None:
    name = EntityInput.single_line(name)
    if name != entity.name:
        entity.aliases = sorted({*entity.aliases, entity.name} - {name})
        entity.name = name


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
    lines = ["", "## Coppermind's Notes", ""]
    facts = active_facts(entity, ledger)
    groups = (
        ("Facts", [f for f in facts if not f.target and f.predicate != "interest"]),
        ("Relationships", [f for f in facts if f.target]),
        ("Interests", [f for f in facts if not f.target and f.predicate == "interest"]),
    )
    for heading, members in groups:
        if not members:
            continue
        lines += [f"### {heading}", ""]
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
                "attended a meeting for",
                "met with",
                "domain",
            }:
                lines.append("  - Other recorded values are shown with their evidence.")
            if not search:
                ev = fact.evidence
                lines.append(f"  - Evidence: [[{ev.path.removesuffix('.md')}]] {label(ev.date)}")
                if ev.quote:
                    lines.append(f"  - {label(ev.quote)}")
        lines.append("")
    if not search:
        paths = sorted({f.evidence.path for f in facts if f.evidence.note_id})
        if paths:
            lines += ["### Supporting notes", ""]
            lines += [f"- [[{p.removesuffix('.md')}]]" for p in paths]
            lines.append("")
        if facts:
            lines += ["Edit a fact above to correct it; delete its line to withdraw it.", ""]
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
        stem = sanitize_stem(request.name)[:90]
        while len(stem.encode("utf-8")) > 220:
            stem = stem[:-1]
        path = f"{folder}/{stem}.md"
        if (
            any(e.path.casefold() == path.casefold() for e in ledger.entities.values())
            or safe_path(self.store.notes_root, path).exists()
        ):
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

    async def notes(self) -> dict[str, NoteRef]:
        async with self.store.session_factory() as session:
            rows = await session.execute(
                sa.select(
                    Note.id, Note.title, Note.path, Note.content_hash, Note.frontmatter
                ).where(Note.reviewed.is_(True), Note.state == "ok")
            )
            return {
                row.id: NoteRef(
                    row.id, row.title or row.path, row.path, row.content_hash, row.frontmatter
                )
                for row in rows
            }

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
            previous.withdrawn_for = None
        entity.facts.append(fact)
        return fact

    def observe(self, ledger: EntityLedger, entity: Entity) -> bytes | None:
        path = safe_path(self.store.notes_root, entity.path)
        if not path.exists():
            if entity.generated is not None and not entity.retired_at:
                entity.retired_at = datetime.now(UTC)
                self.receipt(ledger, entity, "page deleted")
            return None
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if "\0" in text:
            raise ValueError("dossier contains NUL characters")
        fields, _ = fm.parse(text)
        legacy_id = fields.get("entity_id")
        if legacy_id and legacy_id != entity.id:
            raise ValueError("dossier frontmatter belongs to another entity")
        head, block = dossier_parts(text, entity)
        prior = entity.projection_fields or {}
        name = fields.get("name")
        if (
            isinstance(name, str)
            and name.strip()
            and name != prior.get("name")
            and name != entity.name
        ):
            rename(entity, name)
            self.receipt(ledger, entity, "page name")
        listed = fields.get("domains")
        if entity.kind == "company" and isinstance(listed, list) and listed != prior.get("domains"):
            wanted = {
                item.strip().lower()
                for item in listed
                if isinstance(item, str) and DOMAIN.fullmatch(item.strip().lower())
            }
            held = {f.value for f in active_facts(entity, ledger) if f.predicate == "domain"}
            shown = prior.get("domains")
            shown = set(shown) if isinstance(shown, list) else set()
            added = sorted(wanted - held)
            for value in added:
                self.correction(entity, "domain", value, raw)
            removed = [
                fact
                for fact in entity.facts
                if fact.predicate == "domain"
                and fact.state == "confirmed"
                and fact.value in shown
                and fact.value not in wanted
            ]
            for fact in removed:
                fact.state, fact.withdrawn_for = "withdrawn", None
            if added or removed:
                self.receipt(ledger, entity, "page domains")
        entity.personal = owner_wrote(head)
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
                    fact.withdrawn_for = None
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
        return raw

    def prefix(
        self,
        entity: Entity,
        raw: bytes | None,
        ledger: EntityLedger,
        owners: dict[str, set[str]] | None = None,
    ) -> str:
        if raw is None:
            return header(entity, ledger, owners)
        head, _ = dossier_parts(raw.decode(), entity)
        head = remove_legacy_identity(head, entity)
        head = regenerate(entity, head)
        fields, _ = fm.parse(head)
        prior = entity.projection_fields or {}
        wanted = properties(entity, ledger, owners)
        patch = {
            key: value
            for key, value in wanted.items()
            if key not in fields or (key in prior and fields[key] == prior[key])
        }
        for key, value in patch.items():
            if key not in fields:
                head = fm.fill_missing(head, {key: value})
            elif fields[key] != value:
                head = fm.replace_value_in_place(head, key, value) or head
        if "## Scott's Notes" not in head:
            head += "\n## Scott's Notes\n\n"
        if head and not head.endswith("\n"):
            head += "\n"
        return head + dossier_marker(entity.id)

    def project(
        self,
        ledger: EntityLedger,
        entity: Entity,
        seen: dict[str, bytes | None] | None = None,
        owners: dict[str, set[str]] | None = None,
    ) -> None:
        owners = domain_owners(ledger) if owners is None else owners
        try:
            if seen is not None and entity.id in seen:
                raw = seen[entity.id]
            else:
                raw = self.observe(ledger, entity)
                if seen is not None:
                    seen[entity.id] = raw
            if entity.retired_at and raw is None:
                return
            block = render(entity, ledger)
            wanted = (self.prefix(entity, raw, ledger, owners) + block).encode()
            if raw == wanted:
                entity.problem = None
                entity.projection_fields = properties(entity, ledger, owners)
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
            entity.projection_fields = properties(entity, ledger, owners)
        except (OSError, ValueError, UnicodeError, fm.FrontmatterError):
            entity.problem = (
                "Dossier page could not be projected; check its identity, delimiter and path"
            )

    def contribute(
        self,
        ledger: EntityLedger,
        notes: dict[str, NoteRef],
        source_versions: dict[str, dict[str, int]] | None = None,
    ) -> None:
        assignment = self.store.context.config().assignments["enrichment"]
        fields = {"attendees", assignment.attendees_field, assignment.contacts_field} - {"", None}
        attendee_fields = {"attendees", assignment.attendees_field} - {"", None}
        by_email = {
            address: entity
            for entity in ledger.entities.values()
            if not entity.redirect
            for address in entity.emails
        }
        by_path = {e.path.removesuffix(".md"): e for e in ledger.entities.values()}
        by_company: dict[str, list[Entity]] = {}
        for known in ledger.entities.values():
            if known.kind == "company":
                for name in {known.name.casefold(), *(a.casefold() for a in known.aliases)}:
                    by_company.setdefault(name, []).append(known)
        for note in notes.values():
            mentions: dict[str, Entity] = {}
            attendees: dict[str, Entity] = {}
            for key, values in note.frontmatter.items():
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, str):
                        continue
                    entity: Entity | None = None
                    if value.startswith("[[") and value.endswith("]]"):
                        entity = by_path.get(value[2:-2].split("|", 1)[0].removesuffix(".md"))
                    elif key in fields and (address := email(value)):
                        entity = by_email.get(address)
                        if entity is None and digest(address) not in ledger.suppressed:
                            entity = self.make(
                                ledger,
                                EntityInput(
                                    kind="person",
                                    name=email_name(address),
                                    emails=[address],
                                    aliases=[address],
                                ),
                                automatic=True,
                            )
                            by_email[address] = entity
                    while entity and entity.redirect:
                        entity = ledger.entities.get(entity.redirect)
                    if entity and not entity.retired_at:
                        mentions[entity.id] = entity
                        if key in attendee_fields and entity.kind == "person":
                            attendees[entity.id] = entity
            accounts = note.frontmatter.get(self.store.control.schema().role("account_key"), [])
            if isinstance(accounts, str):
                accounts = [accounts]
            companies: dict[str, Entity] = {}
            if isinstance(accounts, list):
                for account in accounts:
                    if not isinstance(account, str) or not account.strip():
                        continue
                    name = account.strip()
                    if len(name) > 200 or any(c in name for c in "\n\r\0"):
                        continue
                    if name.startswith("[[") and name.endswith("]]"):
                        target = by_path.get(name[2:-2].split("|", 1)[0].removesuffix(".md"))
                        if target and target.kind == "company" and not target.retired_at:
                            companies[target.id] = target
                            mentions[target.id] = target
                        continue
                    matches: dict[str, Entity] = {}
                    for match in by_company.get(name.casefold(), []):
                        while match.redirect and match.redirect in ledger.entities:
                            match = ledger.entities[match.redirect]
                        matches[match.id] = match
                    live = [m for m in matches.values() if not m.retired_at and not m.redirect]
                    if len(live) > 1 or (matches and not live):
                        continue
                    if live:
                        company = live[0]
                    elif digest(["company", name, note.id]) not in ledger.suppressed:
                        company = self.make(
                            ledger, EntityInput(kind="company", name=name), automatic=True
                        )
                        by_company.setdefault(name.casefold(), []).append(company)
                    else:
                        continue
                    companies[company.id] = company
                    mentions[company.id] = company
            for person in attendees.values():
                for company in companies.values():
                    for subject, other, predicate in (
                        (person, company, "attended a meeting for"),
                        (company, person, "met with"),
                    ):
                        identity = digest(
                            [note.id, note.content_hash, subject.id, other.id, "account-v1"]
                        )
                        if recorded(
                            subject, identity, note, "account-v1", predicate, other.name, other.id
                        ):
                            continue
                        subject.facts.append(
                            Fact(
                                id=identity,
                                predicate=predicate,
                                value=other.name,
                                target=other.id,
                                state="confirmed",
                                origin="metadata",
                                created_at=datetime.now(UTC),
                                evidence=Evidence(
                                    note_id=note.id,
                                    note_hash=note.content_hash,
                                    path=note.path,
                                    date=str(note.frontmatter.get("date", "")),
                                    extraction_version="account-v1",
                                ),
                            )
                        )
                        subject.version += 1
            for company in companies.values():
                for person in attendees.values():
                    for address in person.emails:
                        domain = address.rpartition("@")[2].lower()
                        if not domain_belongs(domain, company):
                            continue
                        identity = digest(
                            [note.id, note.content_hash, company.id, domain, "domain-v1"]
                        )
                        if any(
                            f.id == identity
                            or (
                                f.predicate == "domain"
                                and f.value == domain
                                and (f.state == "confirmed" or f.withdrawn_for is None)
                                and not (
                                    f.state == "confirmed"
                                    and f.evidence.note_id == note.id
                                    and f.evidence.note_hash != note.content_hash
                                )
                            )
                            for f in company.facts
                        ):
                            continue
                        company.facts.append(
                            Fact(
                                id=identity,
                                predicate="domain",
                                value=domain,
                                state="confirmed",
                                origin="metadata",
                                created_at=datetime.now(UTC),
                                evidence=Evidence(
                                    note_id=note.id,
                                    note_hash=note.content_hash,
                                    path=note.path,
                                    date=str(note.frontmatter.get("date", "")),
                                    quote=address,
                                    extraction_version="domain-v1",
                                ),
                            )
                        )
                        company.version += 1
            for entity in mentions.values():
                identity = digest([note.id, note.content_hash, entity.id, "metadata-v1"])
                if recorded(entity, identity, note, "metadata-v1", "mentioned", note.title):
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
                if not fact.evidence.note_id:
                    continue
                supporting = notes.get(fact.evidence.note_id)
                sources_changed = bool(fact.evidence.source_versions) and (
                    (source_versions or {}).get(fact.evidence.note_id)
                    != fact.evidence.source_versions
                )
                if (
                    supporting is None
                    or supporting.content_hash != fact.evidence.note_hash
                    or sources_changed
                ):
                    if fact.state == "confirmed":
                        fact.state = "withdrawn"
                        fact.withdrawn_for = "evidence"
                        entity.version += 1
                    continue
                if fact.state == "withdrawn" and fact.withdrawn_for == "evidence":
                    fact.state = "confirmed"
                    fact.withdrawn_for = None
                    entity.version += 1
                if fact.state == "confirmed" and fact.evidence.path != supporting.path:
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
            metadata = [
                json.dumps(value, ensure_ascii=False, default=str)
                for value in note.frontmatter.values()
            ]
            for entity in ledger.entities.values():
                for fact in entity.facts:
                    ev = fact.evidence
                    texts = metadata if fact.origin == "metadata" else [note.body]
                    if (
                        ev.note_id == note.id
                        and ev.note_hash == before_hash
                        and (
                            not ev.quote
                            or ev.source_versions
                            or any(ev.quote in text for text in texts)
                        )
                    ):
                        ev.note_hash, ev.path = note.content_hash, note.path
                        entity.version += 1
                        changed = True
            if changed:
                self.save(ledger)
            for job in self.store.context.jobs():
                if (
                    job.purpose == "dossier"
                    and job.note_id == note.id
                    and job.note_hash == before_hash
                ):
                    job.note_hash, job.path = note.content_hash, note.path
                    self.store.context.save_job(job)

    def purge_pending(self) -> bool:
        from coppermind_store.purge import records

        return any(r["state"] in {"applying", "failed"} for r in records(self.store))

    def move_checkpoint(self, step: str) -> None:
        """Test seam for interruption between durable migration steps."""

    async def migrate_page_paths(self, ledger: EntityLedger) -> dict[str, str]:
        """Resume journaled email-page moves while the filing lock is held."""
        index: dict[str, dict[str, set[str]]] | None = None
        unreadable: list[str] = []
        blocked: dict[str, str] = {}
        for entity in list(ledger.entities.values()):
            if entity.retired_at or entity.redirect:
                continue
            if entity.page_move is None:
                old_stem = Path(entity.path).stem
                legacy = any(
                    old_stem == f"{sanitize_stem(address)[:90]} {entity.id}"
                    for address in entity.emails
                )
                if not legacy or any(
                    receipt.get("entity_id") == entity.id and receipt.get("action") == "page moved"
                    for receipt in ledger.receipts
                ):
                    continue
                folder = "People" if entity.kind == "person" else "Companies"
                stem = sanitize_stem(entity.name)[:90]
                while len(stem.encode("utf-8")) > 210:
                    stem = stem[:-1]
                new = f"{folder}/{stem}.md"
                if (
                    any(
                        e.id != entity.id and e.path.casefold() == new.casefold()
                        for e in ledger.entities.values()
                    )
                    or safe_path(self.store.notes_root, new).exists()
                ):
                    new = f"{folder}/{stem} {entity.id}.md"
                old_file = safe_path(self.store.notes_root, entity.path)
                if not old_file.exists():
                    continue
                original = old_file.read_bytes()
                if index is None:
                    index, unreadable = await self.link_index(ledger)
                old_target = entity.path.removesuffix(".md")
                holders = [
                    index[target]
                    for target in (old_target, Path(old_target).name)
                    if target in index
                ]
                pages = sorted(set(unreadable).union(*(h["blocked"] for h in holders)))
                if pages:
                    blocked[entity.id] = (
                        "These pages could not be updated or scanned for links to this dossier: "
                        + ", ".join(pages[:3])
                        + (f" and {len(pages) - 3} more" if len(pages) > 3 else "")
                        + "; resolve them and the move resumes"
                    )
                    continue
                linked_notes = set().union(*(h["note"] for h in holders))
                linked_dossiers = set().union(*(h["dossier"] for h in holders)) - {entity.id}
                notes = sorted(
                    {f.evidence.note_id for f in entity.facts if f.evidence.note_id}
                    | set(linked_notes)
                )
                dossiers = sorted(
                    {
                        e.id
                        for e in ledger.entities.values()
                        if e.id != entity.id and any(f.target == entity.id for f in e.facts)
                    }
                    | set(linked_dossiers)
                )
                entity.page_move = {
                    "old": entity.path,
                    "new": new,
                    "original": base64.b64encode(original).decode(),
                    "notes": notes,
                    "dossiers": dossiers,
                    "snapshots": {},
                }
                self.save(ledger)
                self.move_checkpoint("intent")
            plan = entity.page_move
            old, new = plan["old"], plan["new"]
            old_file = safe_path(self.store.notes_root, old)
            new_file = safe_path(self.store.notes_root, new)
            original = base64.b64decode(plan["original"], validate=True)
            if not new_file.exists():
                if not old_file.exists() or old_file.read_bytes() != original:
                    entity.problem = (
                        "Dossier changed during its move; retry after resolving the copies"
                    )
                    continue
                create_exclusive_bytes(new_file, original)
            elif new_file.read_bytes() != original:
                entity.problem = "Dossier move destination changed; resolve the copies"
                continue
            self.move_checkpoint("copy")
            for note_id in plan["notes"]:
                await self._rewrite_move_note(ledger, entity, note_id)
            for identity in plan["dossiers"]:
                other = ledger.entities.get(identity)
                if other and not other.retired_at:
                    self._rewrite_move_dossier(ledger, entity, other)
            self.move_checkpoint("links")
            atomic_write_text(
                self.root / "moves" / f"{entity.id}.json",
                json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n",
            )
            if old_file.exists():
                if old_file.read_bytes() != original or new_file.read_bytes() != original:
                    entity.problem = "Dossier changed during its move; resolve the copies"
                    continue
                old_file.unlink()
                sync_directory(old_file.parent)
            self.move_checkpoint("remove")
            entity.path, entity.page_move, entity.problem = new, None, None
            self.receipt(ledger, entity, "email page migrated", old_path=old)
            self.save(ledger)
            self.move_checkpoint("complete")
        return blocked

    async def link_index(
        self, ledger: EntityLedger
    ) -> tuple[dict[str, dict[str, set[str]]], list[str]]:
        root = self.store.notes_root
        async with self.store.session_factory() as session:
            rows = (await session.execute(sa.select(Note.id, Note.path, Note.state))).all()
        candidates = [
            ("note" if row.state == "ok" else "blocked", row.id, row.path) for row in rows
        ] + [("dossier", e.id, e.path) for e in ledger.entities.values() if not e.retired_at]

        def scan() -> tuple[dict[str, dict[str, set[str]]], list[str]]:
            index: dict[str, dict[str, set[str]]] = {}
            unreadable: list[str] = []
            for kind, identity, relative in candidates:
                try:
                    text = safe_path(root, relative).read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except (OSError, UnicodeError):
                    unreadable.append(relative)
                    continue
                except ValueError:
                    continue
                for target in {item.removesuffix(".md") for item in WIKI_TARGET.findall(text)}:
                    index.setdefault(target, {"note": set(), "dossier": set(), "blocked": set()})[
                        kind
                    ].add(relative if kind == "blocked" else identity)
            return index, unreadable

        return await asyncio.to_thread(scan)

    def _move_replace(
        self, ledger: EntityLedger, entity: Entity, path: Path
    ) -> tuple[bytes, bytes]:
        plan = entity.page_move
        assert plan is not None
        for _ in range(5):
            raw = path.read_bytes()
            changed = rewrite_dossier_links(raw.decode("utf-8"), plan["old"], plan["new"]).encode(
                "utf-8"
            )
            if raw == changed:
                return raw, raw
            plan["snapshots"][str(path.relative_to(self.store.notes_root))] = base64.b64encode(
                raw
            ).decode()
            self.save(ledger)
            staged = stage_bytes(path, changed)
            try:
                if path.read_bytes() != raw:
                    continue
                commit_staged(staged, path)
                return raw, changed
            finally:
                staged.unlink(missing_ok=True)
        raise ValueError("A linked page kept changing during the dossier move")

    async def _rewrite_move_note(self, ledger: EntityLedger, entity: Entity, identity: str) -> None:
        from coppermind_store.indexer import index_note
        from coppermind_store.notes import _jsonable, _mirror_columns, _title_of

        plan = entity.page_move
        assert plan is not None

        async with (
            self.store._lock_for(identity),
            self.store.session_factory() as session,
            session.begin(),
        ):
            row = await session.get(Note, identity)
            if row is None or row.state != "ok":
                return
            path = safe_path(self.store.notes_root, row.path)
            before, after = self._move_replace(ledger, entity, path)
            if before == after:
                snapshot = plan["snapshots"].get(row.path)
                if not snapshot:
                    return
                saved = base64.b64decode(snapshot, validate=True)
                if (
                    rewrite_dossier_links(saved.decode(), plan["old"], plan["new"]).encode()
                    != after
                ):
                    return
                before = saved
            fields, body = fm.parse(after.decode("utf-8"))
            digest_after = content_hash(after)
            now = datetime.now(UTC)
            await session.execute(
                sa.update(Note)
                .where(Note.id == identity)
                .values(
                    content_hash=digest_after,
                    size_bytes=len(after),
                    mtime=now,
                    frontmatter=_jsonable(fields),
                    **_mirror_columns(fields, self.store.control.schema()),
                    updated_at=now,
                )
            )
            await index_note(
                session,
                note_id=identity,
                path=row.path,
                title=_title_of(body, path),
                body=body,
                version=digest_after,
                now=now,
                notes_folder=self.store.control.settings().notes.notes_folder,
            )
            for subject in ledger.entities.values():
                for fact in subject.facts:
                    if (
                        fact.evidence.note_id == identity
                        and fact.evidence.note_hash == content_hash(before)
                    ):
                        fact.evidence.note_hash = digest_after
                        fact.evidence.quote = rewrite_dossier_links(
                            fact.evidence.quote,
                            plan["old"],
                            plan["new"],
                        )
            for job in self.store.context.jobs():
                if job.note_id == identity and job.note_hash == content_hash(before):
                    job.note_hash = digest_after
                    self.store.context.save_job(job)
            self.save(ledger)

    def _rewrite_move_dossier(self, ledger: EntityLedger, entity: Entity, other: Entity) -> None:
        plan = entity.page_move
        assert plan is not None
        path = safe_path(self.store.notes_root, other.path)
        if not path.exists():
            return
        before, after = self._move_replace(ledger, entity, path)
        if before == after:
            snapshot = plan["snapshots"].get(other.path)
            if snapshot:
                saved = base64.b64decode(snapshot, validate=True)
                if (
                    rewrite_dossier_links(saved.decode(), plan["old"], plan["new"]).encode()
                    == after
                ):
                    before = saved
        if before != after and other.generated is not None:
            try:
                _, old_block = dossier_parts(before.decode(), other)
                _, new_block = dossier_parts(after.decode(), other)
            except ValueError:
                return
            if other.generated == old_block:
                other.generated = new_block
                self.save(ledger)

    async def refresh_locked(self, ledger: EntityLedger) -> None:
        if self.purge_pending():
            raise ValidationFailed(["Finish or abandon the interrupted retention purge first"])
        before = ledger.model_dump_json()
        for entity in ledger.entities.values():
            if entity.kind == "person" and entity.automatic and entity.name in entity.emails:
                address = entity.name
                entity.name = email_name(address)
                entity.aliases = sorted(set(entity.aliases + [address]))
                self.receipt(ledger, entity, "readable email name")
        # Obsidian moves a page without changing its identity. Find missing pages by
        # their entity_id before treating absence as a deletion.
        missing = {
            e.id: e
            for e in ledger.entities.values()
            if e.generated is not None
            and not e.retired_at
            and e.page_move is None
            and not safe_path(self.store.notes_root, e.path).exists()
        }
        found: dict[str, list[str]] = {identity: [] for identity in missing}
        guessed: dict[str, list[str]] = {identity: [] for identity in missing}
        ambiguous: set[str] = set()
        if missing:
            from coppermind_store.reconciler import _IGNORED_DIRECTORIES

            trash = self.store.control.settings().notes.trash_folder.strip("/")
            root = self.store.notes_root
            for parent, directories, files in os.walk(root, followlinks=False):
                relative_parent = Path(parent).relative_to(root).as_posix()
                directories[:] = [
                    name
                    for name in directories
                    if name not in _IGNORED_DIRECTORIES
                    and (name if relative_parent == "." else f"{relative_parent}/{name}") != trash
                ]
                for name in files:
                    if not name.endswith(".md"):
                        continue
                    path = Path(parent) / name
                    try:
                        if path.is_symlink():
                            continue
                        text = path.read_text()
                        fields, _ = fm.parse(text)
                        markers = list(MARKER.finditer(text))
                        if len(markers) > 1:
                            continue
                        identity = markers[0].group(1) if markers else None
                        legacy_id = fields.get("entity_id")
                        if identity and legacy_id and identity != legacy_id:
                            continue
                        identity = identity or legacy_id
                        relative = path.relative_to(root)
                        if isinstance(identity, str) and identity in found:
                            found[identity].append(relative.as_posix())
                        elif not identity and relative.parts[0] in FOLDERS:
                            matches = [
                                e.id
                                for e in ledger.entities.values()
                                if e.generated
                                and text.endswith(e.generated)
                                and matches_identity(
                                    fields,
                                    e,
                                    company_domains(e, ledger) if e.kind == "company" else [],
                                )
                            ]
                            if len(matches) > 1:
                                ambiguous.update(i for i in matches if i in found)
                            elif matches and matches[0] in guessed:
                                guessed[matches[0]].append(relative.as_posix())
                    except (OSError, UnicodeError, fm.FrontmatterError):
                        continue
            for identity, paths in found.items():
                paths[:] = paths or guessed[identity]
                entity = missing[identity]
                if identity in ambiguous:
                    entity.problem = (
                        "Dossier identity matches multiple pages; resolve the ambiguity"
                    )
                    continue
                if len(paths) == 1:
                    old = entity.path
                    entity.path = paths[0]
                    old_stem = Path(old).stem
                    new_stem = Path(entity.path).stem
                    if old_stem != new_stem:
                        rename(entity, new_stem)
                    self.receipt(ledger, entity, "page moved", old_path=old)
                elif len(paths) > 1:
                    entity.problem = "Multiple pages have this dossier identity; resolve the copies"
        # Import device corrections before adding/retracting metadata contributions.
        seen: dict[str, bytes | None] = {}
        for entity in list(ledger.entities.values()):
            if entity.page_move is not None:
                continue
            if entity.id in ambiguous:
                continue
            if entity.id in found and len(found[entity.id]) > 1:
                continue
            try:
                seen[entity.id] = self.observe(ledger, entity)
            except (OSError, ValueError, UnicodeError, fm.FrontmatterError):
                entity.problem = "Dossier page identity, delimiter or path needs repair"
        notes = await self.notes()
        source_notes = {
            fact.evidence.note_id
            for entity in ledger.entities.values()
            for fact in entity.facts
            if fact.evidence.source_versions and fact.evidence.note_id in notes
        }
        versions = {}
        for identity in source_notes:
            try:
                versions[identity] = (await self.store.context.selected_sources(identity))[0]
            except StoreError:
                continue
        self.contribute(ledger, notes, versions)
        await self.migrate(ledger)
        owners = domain_owners(ledger)
        for entity in ledger.entities.values():
            if entity.page_move is not None:
                continue
            if entity.id in ambiguous:
                continue
            if entity.id in found and len(found[entity.id]) > 1:
                continue
            self.project(ledger, entity, seen, owners)
        if ledger.model_dump_json() != before:
            self.save(ledger)
        await self.mirror(ledger, seen)

    async def refresh(self) -> None:
        async with self.store.maintenance.shared(), self.store._filing_lock, self.lock:
            if self.purge_pending():
                return
            ledger = self.load()
            await self.refresh_locked(ledger)
            revision = ledger.revision
            blocked = await self.migrate_page_paths(ledger)
            if ledger.revision != revision:
                await self.refresh_locked(ledger)
            if blocked:
                for identity, problem in blocked.items():
                    ledger.entities[identity].problem = problem
                self.save(ledger)

    def personal_text(self, entity: Entity, seen: dict[str, bytes | None] | None = None) -> str:
        if seen is not None and entity.id in seen:
            raw = seen[entity.id]
            if not raw:
                return ""
            try:
                return fm.parse(dossier_parts(raw.decode(), entity)[0])[1]
            except (ValueError, UnicodeError, fm.FrontmatterError):
                return ""
        try:
            text = safe_path(self.store.notes_root, entity.path).read_text()
            fields, _ = fm.parse(text)
            if fields.get("entity_id") not in (None, entity.id):
                return ""
            _, body = fm.parse(dossier_parts(text, entity)[0])
            return body
        except (OSError, ValueError, UnicodeError, fm.FrontmatterError):
            return ""

    async def mirror(
        self, ledger: EntityLedger, seen: dict[str, bytes | None] | None = None
    ) -> None:
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
                    else self.personal_text(entity, seen) + render(entity, ledger, search=True)
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
                rename(entity, change.name or "")
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
                fact.withdrawn_for = None
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
                fact.withdrawn_for = None
            if change.emails:
                if not set(change.emails) <= set(entity.emails):
                    raise ValidationFailed(["split emails must belong to the original dossier"])
                target.emails = sorted(set(change.emails))
                entity.emails = [a for a in entity.emails if a not in change.emails]
        else:
            assert target is not None
            positions = {fact.id: index for index, fact in enumerate(target.facts)}
            for fact in selected:
                index = positions.get(fact.id)
                if index is None:
                    positions[fact.id] = len(target.facts)
                    target.facts.append(fact.model_copy(deep=True))
                elif target.facts[index].state == "withdrawn" and fact.state == "confirmed":
                    # A split leaves a withdrawn copy behind; merging back restores the live one.
                    target.facts[index] = fact.model_copy(deep=True)
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
                if len(list(MARKER.finditer(text))) != 1:
                    continue
                try:
                    head, block = dossier_parts(text, entity)
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
        for snapshot in (self.root / "moves").glob("*.json"):
            snapshot.unlink()
        for identity in entity_ids:
            entity = ledger.entities.pop(identity, None)
            if entity:
                ledger.suppressed.extend(digest(address) for address in entity.emails)
                ledger.suppressed.extend(
                    digest([entity.kind, entity.name, f.evidence.note_id])
                    for f in entity.facts
                    if f.evidence.note_id
                )
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
            and r.get("note_id") not in note_ids
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
                continue
            dossiers = job.evidence.get("dossiers", [])
            if any(
                d["id"] in entity_ids
                or any(f["evidence"].get("note_id") in note_ids for f in d.get("facts", []))
                for d in dossiers
            ):
                # Context snapshots must not keep quotations from purged supporting notes.
                job.evidence.pop("dossiers", None)
                job.result = None
                job.pending_approval = None
                job.decisions = []
                job.state = "stale"
                job.set_error("Supporting dossier evidence was purged; regenerate suggestions")
                self.store.context.save_job(job)
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
