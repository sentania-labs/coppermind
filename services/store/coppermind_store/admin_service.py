"""What Admin asks the store for, in one place.

Admin mounts only the control state, so anything about notes, recordings, the
calendar or people is asked for here. Numbers that more than one page shows are
worked out once, in `overview`, and every page reads them from there: the menu
adds up to Home because both are the same answer.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coppermind.api_keys import (
    COMPANION_PERMISSIONS,
    add_key,
    describe,
    load_keys,
    permissions_of,
    scopes_for,
)
from coppermind.atomicio import atomic_write_bytes, atomic_write_text
from coppermind.context_config import ContextConfig
from coppermind.naming import sanitize_folder
from coppermind.settings import ProductSettings
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import MoveNote, SearchQuery, StoreError
from coppermind_store.admin_calendar import AdminCalendar
from coppermind_store.admin_features import Features
from coppermind_store.admin_incoming import AdminIncoming
from coppermind_store.admin_notes import AdminNotes
from coppermind_store.admin_people import AdminPeople
from coppermind_store.admin_storage import AdminStorage
from coppermind_store.ai_services import Refused

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

SYNC_TEST_NAME = "Coppermind sync test.md"
PLUGIN_FOLDER = ".obsidian/plugins/coppermind-review"
PLUGIN_FILES = ("main.js", "manifest.json", "styles.css")
PAIRING_MINUTES = 10
# Wrong guesses one client may make at a pairing code, and all clients together, before the
# code stops working for them. A six digit code guessed a hundred times is found one time
# in ten thousand.
PAIRING_ATTEMPTS = 10
PAIRING_ATTEMPTS_IN_ALL = 100
# The longest one client's tries may queue up before a further try is turned away at once.
PAIRING_QUEUE_SECONDS = 10
NEW_DROP_FOLDER = "To Coppermind"
# Problems that are not anybody's to fix: they are told once and can be put away.
FILING_ACTIONS = {"note-check", "note-keep-both", "note-replace", "note-no-time"}
NOTICE_KINDS = {"returned_note", "folder_kept", "reserved_type", "date_mismatch", "path_notice"}
FOLDER_SETTINGS = {
    "review": "review_folder",
    "filed": "notes_folder",
    "files": "artifacts_folder",
    "journal": "journal_folder",
    "trash": "trash_folder",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


class AdminService:
    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self.context = store.context
        self.features = Features(self.context)
        self.notes = AdminNotes(store)
        self.incoming = AdminIncoming(store)
        self.calendar = AdminCalendar(store)
        self.people = AdminPeople(store)
        self.storage = AdminStorage(store, self.notes)
        self._pair_lock = asyncio.Lock()
        # When each client may next try a pairing code: no faster than one try a second each.
        self._pair_next: dict[str, float] = {}

    @property
    def state_dir(self) -> Path:
        return self.store.control.store.state_dir

    # -- startup -------------------------------------------------------------------------

    async def startup(self) -> None:
        """Switch the note workflow on, which the redesigned Admin has no separate step for."""
        async with self.context.lock:
            config = self.context.config()
            if config.lifecycle_enabled:
                return
            self.features.ensure_owned_properties()
            fresh = (
                config.dropbox_folder == "Dropbox"
                and not (self.store.notes_root / "Dropbox").exists()
                and not self.context.dropbox.records()["items"]
                and not self.context.dropbox.records()["history"]
            )
            if fresh:
                # A new install gets a folder name that does not read as another product.
                config.dropbox_folder = NEW_DROP_FOLDER
            config.lifecycle_enabled = True
            self.context.save_config(ContextConfig.model_validate(config.model_dump()))

    # -- small durable records -----------------------------------------------------------

    def log(self, text: str) -> None:
        """One line for Home's "Recently": something that happened, in a sentence."""
        entries = self.context.read("admin-activity", [])
        entries.insert(0, {"at": now(), "text": text})
        self.context.write("admin-activity", entries[:50])

    def setup_state(self) -> dict[str, Any]:
        return self.context.read("admin-setup", {"skipped": {}})

    async def setup_finished(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Setup is finished and its owner has been told, so the guide stops saying so."""
        state = self.setup_state()
        if not state.get("finished_seen"):
            state["finished_seen"] = True
            self.context.write("admin-setup", state)
        return state

    async def setup_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Something Admin itself saw happen, for Home's "Recently"."""
        text = " ".join(str(payload.get("text", "")).split())[:300]
        if text:
            self.log(text)
        return {}

    async def setup_skip(self, payload: dict[str, Any]) -> dict[str, Any]:
        step = str(payload["step"])
        if step not in {"sugg", "trans", "cal"}:
            raise ValueError("that step cannot be skipped")
        state = self.setup_state()
        state["skipped"][step] = True
        self.context.write("admin-setup", state)
        return state

    # -- sync, as the helper last published it -------------------------------------------

    def sync_status(self) -> dict[str, Any]:
        try:
            status = json.loads((self.state_dir / "sync" / "status.json").read_bytes())
        except (OSError, ValueError):
            return {"state": "unknown", "configured": False}
        if not isinstance(status, dict):
            return {"state": "unknown", "configured": False}
        try:
            devices = json.loads((self.state_dir / "sync" / "devices.json").read_bytes())
        except (OSError, ValueError):
            devices = {}
        status["devices"] = devices.get("devices", []) if isinstance(devices, dict) else []
        status["last_change"] = devices.get("last_change") if isinstance(devices, dict) else None
        status["used_bytes"] = devices.get("used_bytes") if isinstance(devices, dict) else None
        status["last_activity_at"] = (
            devices.get("last_activity_at") if isinstance(devices, dict) else None
        )
        return status

    # -- connected apps ------------------------------------------------------------------

    def key_use(self) -> dict[str, str]:
        try:
            return json.loads((self.state_dir / "key-use.json").read_bytes())
        except (OSError, ValueError):
            return {}

    async def key_used(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The API saw this key used. Kept to the minute, which is all the screen shows."""
        key_id = str(payload["key_id"])
        used = self.key_use()
        stamp = now()
        if used.get(key_id, "")[:16] != stamp[:16]:
            used[key_id] = stamp
            await asyncio.to_thread(
                atomic_write_bytes, self.state_dir / "key-use.json", json.dumps(used).encode()
            )
        return {}

    def apps(self) -> list[dict[str, Any]]:
        used = self.key_use()
        return [
            {
                "id": key.key_id,
                "name": key.name,
                "can": describe(key.scopes),
                "permissions": permissions_of(key.scopes),
                "used": used.get(key.key_id, ""),
                "created": key.created_at.isoformat(),
                "starter": key.bootstrap_default,
                "companion": key.companion,
                "device": key.device,
            }
            for key in load_keys(self.store.control.store).keys
            if key.revoked_at is None
        ]

    # -- the companion: put it in the notes filesystem, pair it with a code ---------------

    def companion_placed(self) -> bool:
        return (self.store.notes_root / PLUGIN_FOLDER / "main.js").is_file()

    async def companion_install(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write the companion add-on where Obsidian looks for it, with this server's address."""
        files = payload["files"]
        folder = self.store.notes_root / PLUGIN_FOLDER
        for name in PLUGIN_FILES:
            data = base64.b64decode(files[name], validate=True)
            await asyncio.to_thread(atomic_write_bytes, folder / name, data)
        settings_file = folder / "data.json"
        saved: dict[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            saved = json.loads(settings_file.read_bytes())
        if not saved.get("url"):
            saved["url"] = str(payload.get("url", "")).rstrip("/")
            await asyncio.to_thread(
                atomic_write_text, settings_file, json.dumps(saved, indent=2) + "\n"
            )
        return {"placed": True}

    def _pairing(self) -> dict[str, Any]:
        return self.context.read("pairing", {})

    async def pairing_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A fresh six-digit code, good for ten minutes and for one device."""
        current = self._pairing()
        fresh = bool(payload.get("fresh"))
        if (
            not fresh
            and current.get("code")
            and not current.get("done")
            and current.get("expires_at", "") > now()
            and current.get("attempts", 0) < PAIRING_ATTEMPTS_IN_ALL
        ):
            return self.pairing_view(current)
        record = {
            "code": f"{secrets.randbelow(1_000_000):06d}",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=PAIRING_MINUTES)).isoformat(),
            "attempts": 0,
            "done": None,
            "started_at": now(),
        }
        self.context.write("pairing", record)
        return self.pairing_view(record)

    @staticmethod
    def pairing_view(record: dict[str, Any]) -> dict[str, Any]:
        live = (
            bool(record.get("code"))
            and not record.get("done")
            and record.get("expires_at", "") > now()
            and record.get("attempts", 0) < PAIRING_ATTEMPTS_IN_ALL
        )
        return {
            "code": record.get("code", "") if live else "",
            "expires_at": record.get("expires_at", ""),
            "done": record.get("done"),
        }

    async def pairing_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.pairing_view(self._pairing())

    async def pair_exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Trade a pairing code typed into the companion for the companion's own key.

        The code is short, so it is guarded: it works once, for ten minutes, a client that
        guesses wrong a handful of times is refused, and each client is answered no faster
        than once a second.
        """
        client = str(payload.get("client") or "unknown")[:64]
        # Each client is slowed down by itself, so one that hammers the door does not make
        # the real device wait behind it.
        if len(self._pair_next) > 2000:
            self._pair_next.clear()
        clock = time.monotonic()
        turn = max(clock, self._pair_next.get(client, 0.0))
        if turn - clock > PAIRING_QUEUE_SECONDS:
            # Already a queue of tries from this client: one more is not made to wait in it.
            raise Refused("code", "Too many tries at once. Wait a few seconds and try again.")
        self._pair_next[client] = turn + 1.0
        if turn > clock:
            await asyncio.sleep(turn - clock)
        async with self._pair_lock:
            record = self._pairing()
            offered = "".join(c for c in str(payload.get("code", "")) if c.isdigit())
            live = self.pairing_view(record)["code"]
            guesses = record.setdefault("by", {})
            spent = guesses.get(client, 0) >= PAIRING_ATTEMPTS
            if spent or not live or not secrets.compare_digest(offered.encode(), live.encode()):
                if live and not spent:
                    guesses[client] = guesses.get(client, 0) + 1
                    record["attempts"] = record.get("attempts", 0) + 1
                    self.context.write("pairing", record)
                raise Refused(
                    "code",
                    "That pairing code was not accepted. Check the code shown in Coppermind "
                    "Admin. If it has run out, press New code there.",
                )
            device = " ".join(str(payload.get("device", "")).split())[:60] or "a device"
            name = f"Obsidian companion on {device}"
            control = self.store.control.store
            for _ in range(3):
                try:
                    credential = add_key(
                        control,
                        name,
                        scopes_for(COMPANION_PERMISSIONS),
                        if_revision=load_keys(control).revision,
                        companion=True,
                        device=device,
                    )
                    break
                except RevisionConflict:
                    continue
            else:
                raise ValueError("The key list is busy. Try the pairing code again.")
            key_id = credential.split("_", 2)[1]
            record["done"] = {"key_id": key_id, "name": name, "device": device, "at": now()}
            self.context.write("pairing", record)
            self.log(f"The Obsidian companion on {device} connected.")
            return {"credential": credential, "name": name}

    # -- the overview every page reads ---------------------------------------------------

    async def overview(self, payload: dict[str, Any]) -> dict[str, Any]:
        problems = await self.store.get_problems()
        services = self.context.services.listing()
        sugg = await self.features.suggestions()
        trans = await self.features.transcription()
        search = await self.features.search()
        incoming = await self.incoming.listing()
        calendar = await self.calendar.attention()
        files = await asyncio.to_thread(self.calendar._files)
        apps = self.apps()
        waiting = await self.notes.waiting()
        cleanup = await self.storage.cleanup_count()
        return {
            "sync": self.sync_status(),
            "services": services,
            "sugg": {key: sugg[key] for key in ("state", "service", "model", "waiting", "week")},
            "trans": {key: trans[key] for key in ("state", "service", "week")},
            "search": {key: search[key] for key in ("state", "service")},
            "notes": {
                "stuck": await self.notes.stuck(problems),
                "unsure": await self.notes.unsure(),
                "waiting": len(waiting),
            },
            "incoming": {
                "failed": self.incoming.failed(incoming),
                "total": len(incoming),
            },
            "calendar": {
                **calendar,
                "files": len(files),
                "events": sum(item["adds"] for item in files),
            },
            "storage": {
                "interrupted": await self.storage.interrupted(),
                "cleanup": cleanup["count"],
                "damaged": [
                    {"reference": problem.reference, "reason": problem.reason}
                    for problem in problems
                    if problem.kind in {"artifact", "damaged_source"}
                ],
            },
            "companion": {
                "placed": await asyncio.to_thread(self.companion_placed),
                "paired": [app for app in apps if app["companion"]],
            },
            "setup": self.setup_state(),
            "notices": self._notices(problems),
            "recent": await self.recent(),
            "drop_folder": self.context.config().dropbox_folder,
            "timezone": self.store.control.settings().general.timezone,
        }

    def _notices(self, problems: list[Any]) -> list[dict[str, Any]]:
        dismissed = set(self.context.read("admin-dismissed", []))
        notices = []
        for problem in problems:
            if problem.kind not in NOTICE_KINDS:
                continue
            key = hashlib.sha256(
                f"{problem.kind}\0{problem.reference}\0{problem.reason}".encode()
            ).hexdigest()[:24]
            if key in dismissed:
                continue
            notices.append(
                {
                    "id": key,
                    "kind": problem.kind,
                    "note_id": problem.note_id or "",
                    "reference": problem.reference,
                    "reason": problem.reason,
                }
            )
        return notices[:5]

    async def notice_dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload["id"])
        problems = await self.store.get_problems()
        target = next((n for n in self._notices(problems) if n["id"] == key), None)
        if target and target["kind"] == "returned_note" and target["note_id"]:
            with contextlib.suppress(StoreError):
                await self.store.dismiss_reconciler_notice(target["note_id"])
        dismissed = self.context.read("admin-dismissed", [])
        if key not in dismissed:
            dismissed.append(key)
        self.context.write("admin-dismissed", dismissed[-500:])
        return {}

    async def recent(self) -> list[dict[str, str]]:
        """The last few things that happened, each one something the system observed."""
        entries = list(self.context.read("admin-activity", []))[:8]
        for job in self.context.recordings.jobs():
            entries.append(
                {
                    "at": job.created_at,
                    "text": f'Recording "{job.title}" arrived'
                    + (
                        " and is being written up."
                        if job.state == "running"
                        else ". It is waiting to be written up."
                        if job.state == "queued"
                        else "."
                    ),
                }
            )
            if job.succeeded_at:
                entries.append(
                    {
                        "at": job.succeeded_at,
                        "text": f'Recording "{job.title}" was written up. '
                        "The note is waiting for your review.",
                    }
                )
        for note in (await self.notes.waiting())[:8]:
            entries.append(
                {
                    "at": note["arrived"],
                    "text": f'New note "{note["title"]}" is waiting for your review.',
                }
            )
        for path in sorted((self.state_dir / "filing").glob("*.json"))[-200:]:
            with contextlib.suppress(OSError, ValueError):
                plan = json.loads(path.read_bytes())
                if plan.get("complete") and plan.get("folder"):
                    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
                    title = Path(str(plan.get("path") or plan.get("note_path") or "")).stem
                    entries.append(
                        {
                            "at": stamp,
                            "text": (
                                f'"{title}" was filed' if title else "A reviewed note was filed"
                            )
                            + f" in {plan['folder']}.",
                        }
                    )
        entries.sort(key=lambda item: item["at"], reverse=True)
        return entries[:4]

    # -- folders -------------------------------------------------------------------------

    async def folders(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The top-level folders a person can see in their notes, for the folder picker."""

        from coppermind_store.entities import FOLDERS as OWN_PAGES

        settings = self.store.control.settings()
        # Folders Coppermind keeps for its own pages and records are never offered as a
        # place to put something else.
        own = {name.casefold() for name in (*OWN_PAGES, settings.notes.sources_folder)}

        def read() -> list[str]:
            try:
                return sorted(
                    (
                        entry.name
                        for entry in self.store.notes_root.iterdir()
                        if entry.is_dir()
                        and not entry.name.startswith(".")
                        and entry.name.casefold() not in own
                    ),
                    key=str.casefold,
                )
            except OSError:
                return []

        async with self.store.session_factory() as session:
            import sqlalchemy as sa

            from coppermind.db.models import Note

            types = sorted(
                {
                    value
                    for value in (
                        await session.scalars(
                            sa.select(Note.type).where(Note.state == "ok").distinct()
                        )
                    ).all()
                    if value
                }
            )
        schema = self.store.control.schema()
        vocabulary: list[Any] = next(
            (
                list(key.vocabulary or [])
                for key in schema.keys
                if key.name == schema.role("type_key")
            ),
            [],
        )
        return {
            "folders": await asyncio.to_thread(read),
            "current": {
                "review": settings.notes.review_folder,
                "filed": settings.notes.notes_folder,
                "files": settings.notes.artifacts_folder,
                "journal": settings.notes.journal_folder,
                "trash": settings.notes.trash_folder,
                "drop": self.context.config().dropbox_folder,
            },
            "dated": settings.notes.dated_types,
            "types": sorted(set(types) | set(vocabulary or [])),
            "waiting": (await self.notes.counts())["waiting"],
        }

    async def folders_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save the folder choices. Notes waiting for review move with their folder."""
        current = (await self.folders({}))["current"]
        wanted = {key: str(payload.get(key, current[key])).strip() for key in current}
        for key, value in wanted.items():
            if not value or sanitize_folder(value) != value or value.startswith("."):
                raise Refused(key, "Pick a folder, or type a name for the new one.")
        from coppermind_store.entities import FOLDERS as OWN_PAGES

        sources = self.store.control.settings().notes.sources_folder
        own = {name.casefold() for name in (*OWN_PAGES, sources)}
        seen: dict[str, str] = {}
        for key, value in wanted.items():
            if value.casefold() in own:
                raise Refused(
                    key,
                    f'"{value}" is a folder Coppermind keeps for its own pages. '
                    "Pick a different folder.",
                )
            if value.casefold() in seen:
                raise Refused(
                    key,
                    f'"{value}" is already used for something else on this page. '
                    "Pick a different folder.",
                )
            seen[value.casefold()] = key
        changed = {key: value for key, value in wanted.items() if value != current[key]}
        dated = payload.get("dated")
        state = self.store.control.store.read("settings")
        body = dict(state.body)
        notes = dict(body.get("notes", {}))
        for key, setting in FOLDER_SETTINGS.items():
            notes[setting] = wanted[key]
        if isinstance(dated, list):
            notes["dated_types"] = [str(item) for item in dated if str(item).strip()]
        body["notes"] = notes
        try:
            ProductSettings.model_validate({k: v for k, v in body.items() if k != "revision"})
        except ValueError as exc:
            raise Refused("review", "Those folders cannot be used together: " + str(exc)) from exc
        self.store.control.store.write("settings", body, if_revision=state.revision)
        if "drop" in changed:
            async with self.context.lock:
                config = self.context.config()
                config.dropbox_folder = wanted["drop"]
                self.context.save_config(ContextConfig.model_validate(config.model_dump()))
        moved = 0
        if "review" in changed:
            old = current["review"]
            for note in await self.notes.waiting():
                if not note["path"].startswith(old + "/"):
                    continue
                inner = str(Path(note["path"]).parent)[len(old) :].strip("/")
                target = wanted["review"] + ("/" + inner if inner else "")
                try:
                    await self.store.move_note(note["id"], MoveNote(target_folder=target), None)
                    moved += 1
                except StoreError:
                    continue
        return {"changed": changed, "was": current, "moved": moved}

    # -- sync helpers the store has to do because it is the one writer -------------------

    async def test_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Put a small note at the top of the notes filesystem to watch it reach a device."""
        zone = self.store.control.settings().general.timezone
        from zoneinfo import ZoneInfo

        stamp = datetime.now(ZoneInfo(zone)).strftime("%b %d at %I:%M %p").replace(" 0", " ")
        text = (
            "# Coppermind sync test\n\n"
            f"Coppermind sent this note on {stamp} so you can see sync working.\n"
            "Seeing it on your phone or computer means notes are reaching you.\n"
            "Delete it whenever you like.\n"
        )
        await asyncio.to_thread(atomic_write_text, self.store.notes_root / SYNC_TEST_NAME, text)
        return {"name": SYNC_TEST_NAME.removesuffix(".md")}

    # -- search --------------------------------------------------------------------------

    async def search_try(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("q") or "").strip()
        if not text:
            return {"hits": []}
        page = await self.store.search(SearchQuery(q=text, limit=8))
        return {
            "hits": [
                {
                    "title": hit.title,
                    "kind": hit.kind,
                    "match": getattr(hit, "match", "keyword"),
                    "path": hit.path,
                }
                for hit in page.items
            ]
        }

    async def search_rebuild(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One rebuild for everything search reads from: the text index and the property index."""
        result = await self.store.rebuild_search_index()
        with contextlib.suppress(Exception):
            await self.store.field_index.rebuild()
        return {"started": result.started, "status": result.status.model_dump(mode="json")}

    async def support(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A summary of this install for support: no note content and no keys."""
        status = await self.store.get_status()
        history = await self.store.get_note_history_status()
        index = await self.store.get_search_index_status()
        config = self.context.config()
        return {
            "counters": status.counters.model_dump(mode="json"),
            "history": history.model_dump(mode="json"),
            "search": index.model_dump(mode="json"),
            "services": [
                {
                    "kind": item["kind"],
                    "answering": item["answering"],
                    "text": item["text"]["ok"],
                    "speech": item["speech"]["ok"],
                    "embed": item["embed"]["ok"],
                    "detail": item["detail"],
                }
                for item in self.context.services.listing()
            ],
            "features": {
                "suggestions": config.assignments["enrichment"].enabled,
                "transcription": bool(
                    config.assignments.get("transcription")
                    and config.assignments["transcription"].enabled
                ),
                "search_by_meaning": config.embeddings.enabled,
            },
            "sync": {
                key: value
                for key, value in self.sync_status().items()
                if key in {"state", "configured", "paused", "simulated", "last_error"}
            },
        }

    # -- removing an AI service that is in use -------------------------------------------

    async def service_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = str(payload["id"])
        service = self.context.services.get(identity)
        takeover = payload.get("takeover") or {}
        for feature in service["used_by"]:
            if not takeover.get(feature):
                raise Refused("rm_" + feature, "Choose one before removing.")
            self.features.check_takeover(feature, str(takeover[feature]), service["id"])
        done = [
            await self.features.take_over(feature, str(takeover[feature]))
            for feature in service["used_by"]
        ]
        async with self.context.lock:
            config = self.context.config()
            self.context.services.forget(config, identity)
            self.context.save_config(config)
        self.log(f'AI service "{service["name"]}" was removed.')
        return {"name": service["name"], "done": done}

    async def service_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.context.services.add(payload)
        self.log(f'AI service "{result["service"]["name"]}" was added.')
        return result

    async def service_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.context.services.test(payload)
        if result["answering"] and any(result.get("resumed", {}).values()):
            self.log(f"{result['service']['name']} is answering again.")
        return result

    async def services(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "services": self.context.services.listing(),
            "suggested_name": self.context.services.suggest_name(),
        }

    async def service(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"service": self.context.services.get(str(payload["id"]))}

    async def calendar_file(self, filename: str, data: bytes) -> dict[str, Any]:
        result = await self.calendar.add(filename, data)
        line = self._happened("calendar-add", result)
        if line:
            self.log(line)
        return result

    # -- dispatch ------------------------------------------------------------------------

    async def act(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one named operation. A refusal a person can act on comes back as data."""
        handler = self._handlers().get(action)
        if handler is None:
            raise KeyError(action)
        try:
            result = await handler(payload)
        except Refused as refusal:
            return refusal.body()
        if isinstance(result, dict):
            line = self._happened(action, result)
            if line:
                self.log(line)
        return result if isinstance(result, dict) else {"items": result}

    @staticmethod
    def _happened(action: str, result: dict[str, Any]) -> str:
        """The line for Home's "Recently" when an action changed something worth recalling."""
        if result.get("refused"):
            return ""
        if action == "suggestions-save" and result.get("was") == "none":
            return f"Suggestions turned on, using {result.get('model') or 'the usual model'}."
        if action == "transcription-save" and result.get("was") == "none":
            return f"Transcription turned on, using {result['service']}."
        if action == "calendar-add" and result.get("state") == "added":
            return f'Calendar file "{result["name"]}" was added.'
        if action in FILING_ACTIONS and result.get("filed"):
            folder = str(Path(result["path"]).parent)
            if result.get("reviewed"):
                return f'You reviewed "{result["name"]}". Filed in {folder}.'
            return f'"{result["name"]}" was filed in {folder}.'
        if action == "note-restore" and result.get("title"):
            return f'You brought back the deleted note "{result["title"]}".'
        if action == "incoming-retry" and result.get("title"):
            return f'You retried the recording "{result["title"]}". It is being written up.'
        if action == "service-save":
            name = result["service"]["name"]
            if result.get("tested") and result.get("answering"):
                return f'AI service "{name}" was changed and tested.'
            if result.get("renamed"):
                return f'AI service renamed to "{name}".'
        return ""

    def _handlers(self) -> dict[str, Any]:
        features, notes, incoming = self.features, self.notes, self.incoming
        calendar, people, storage = self.calendar, self.people, self.storage

        def plain(function: Any) -> Any:
            async def call(payload: dict[str, Any]) -> Any:
                return await function()

            return call

        def off(function: Any) -> Any:
            async def call(payload: dict[str, Any]) -> Any:
                await function()
                return {}

            return call

        return {
            "overview": self.overview,
            "setup-skip": self.setup_skip,
            "setup-finished": self.setup_finished,
            "setup-log": self.setup_log,
            "notice-dismiss": self.notice_dismiss,
            "apps": plain(_wrap(self.apps)),
            "key-used": self.key_used,
            "companion-install": self.companion_install,
            "pairing-start": self.pairing_start,
            "pairing-state": self.pairing_state,
            "pair-exchange": self.pair_exchange,
            "folders": self.folders,
            "folders-save": self.folders_save,
            "test-note": self.test_note,
            "search-try": self.search_try,
            "search-rebuild": self.search_rebuild,
            "support": self.support,
            "services": self.services,
            "service": self.service,
            "service-add": self.service_add,
            "service-test": self.service_test,
            "service-save": self.context.services.save,
            "service-remove": self.service_remove,
            "suggestions": plain(features.suggestions),
            "suggestions-save": features.suggestions_save,
            "suggestions-pause": features.suggestions_pause,
            "suggestions-wait": features.suggestions_wait,
            "suggestions-fill": features.suggestions_fill,
            "suggestions-tick": features.suggestions_tick,
            "suggestions-older": features.suggestions_older,
            "summary-directions": plain(_wrap(features.summary_directions)),
            "summary-directions-save": features.summary_directions_save,
            "note-summary": notes.summary,
            "note-summary-again": notes.summary_again,
            "transcription": plain(features.transcription),
            "transcription-save": features.transcription_save,
            "transcription-pause": features.transcription_pause,
            "transcription-wait": features.transcription_wait,
            "transcription-tries": features.transcription_tries,
            "transcription-retry-failed": features.transcription_retry_failed,
            "search": plain(features.search),
            "search-on": features.search_on,
            "search-off": off(features.search_off),
            "notes-waiting": plain(notes.waiting),
            "notes-deleted": plain(notes.deleted),
            "notes-alone": plain(notes.left_alone),
            "notes-find": lambda payload: notes.find(str(payload.get("q", ""))),
            "note": notes.detail,
            "note-check": notes.check_again,
            "note-no-time": notes.no_set_time,
            "note-gone": notes.gone_on_purpose,
            "note-keep-both": notes.keep_both,
            "note-replace": notes.replace,
            "note-copy": notes.resolve_copy,
            "note-restore": notes.restore,
            "note-delete-for-good": notes.delete_for_good,
            "incoming": plain(incoming.listing),
            "incoming-item": incoming.detail,
            "incoming-retry": incoming.retry,
            "incoming-remove": incoming.remove,
            "incoming-accept": incoming.accept,
            "incoming-replace-failed": incoming.replace_failed,
            "recording-delete": incoming.delete_recording,
            "calendar": calendar.view,
            "calendar-meeting": calendar.meeting,
            "calendar-choose": calendar.choose,
            "calendar-max": calendar.set_max,
            "people": plain(people.listing),
            "person": people.detail,
            "person-remove": people.remove,
            "fact-correct": people.fact_correct,
            "fact-add": people.fact_add,
            "fact-remove": people.fact_remove,
            "voice-remove": people.voice_remove,
            "voice-never": people.voice_never,
            "voice-name": people.voice_name,
            "voice-forget": people.voice_forget,
            "voice-level": people.voice_level,
            "voice-recognition": people.voice_recognition,
            "voice-clip": people.clip,
            "storage": self.storage_view,
            "storage-keep": storage.set_keep,
            "cleanup": plain(storage.cleanup),
            "cleanup-confirm": storage.cleanup_confirm,
            "cleanup-cancel": storage.cleanup_cancel,
            "cleanup-bring-back": storage.bring_back,
            "files": storage.files,
            "file-remove": storage.file_remove,
        }

    async def storage_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        files = await self.storage.files({"shown": 6})
        return {
            "usage": await self.storage.usage(),
            "keep_days": self.storage.keep_days(),
            "cleanup": (await self.storage.cleanup_count())["count"],
            "interrupted": await self.storage.interrupted(),
            "deleted": len(await self.notes.deleted()),
            "files": files["total"],
        }


def _wrap(function: Any) -> Any:
    async def call() -> Any:
        return function()

    return call
