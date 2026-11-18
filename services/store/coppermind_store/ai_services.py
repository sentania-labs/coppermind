"""AI services: one record per server or account, what it can do, and whether it answers.

A service is a `ModelConnection` plus what Coppermind has observed about it. One
test, run when a service is added, edited or tested by hand, tries each of its
models with a made-up meeting and sends a made-up recording, and records what
worked. The features (suggestions, transcription, search by meaning) then only
choose among services whose test showed they can do the job; nobody is asked
for a model name or a reply format the service could have been asked for.

Health is observed and never assumed. Every provider call reports its outcome
here, so "not answering since" is the time of the first call that got no
answer, and it clears on the first call that does.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from coppermind.context_config import FeatureAssignment, ModelConnection
from coppermind.ids import new_id
from coppermind_store.model_provider import ProviderError

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

SAMPLE_AUDIO = Path(__file__).with_name("sample-meeting.mp3")
SAMPLE_AUDIO_SECONDS = 10
# The two voices in the sample say these lines, so the test can show what was heard.
RECORD = "ai-services"
# Every model on the owner's own server may be tried; a handful is enough to choose from.
OWN_MODEL_LIMIT = 8
# An outside account charges for each try, so only a short list is tried there.
OUTSIDE_MODEL_LIMIT = 2
TEST_SILENCE_S = 90
SPEECH_WAIT_S = 120
# Outcomes that mean nothing answered at all, as opposed to an answer that was a refusal.
UNREACHABLE = frozenset(
    {
        "provider-connect",
        "provider-no-response",
        "provider-stalled",
        "provider-dropped",
        "transcription-http",
        "transcription-internal",
    }
)
SPEECH_NAMES = re.compile(r"whisper|transcri|diari|stt|speech|parakeet|canary", re.I)
EMBED_NAMES = re.compile(r"embed|bge|e5|minilm|gte-", re.I)
REPLY_FORMATS: tuple[Literal["diarized_json", "verbose_json"], ...] = (
    "diarized_json",
    "verbose_json",
)
KINDS = {"compatible": "own", "openai": "openai", "anthropic": "anthropic"}
PROVIDERS = {value: key for key, value in KINDS.items()}
FEATURES = ("sugg", "trans", "search")


def now() -> str:
    return datetime.now(UTC).isoformat()


class Refused(Exception):
    """An entry was not accepted. `field` is the form field the reason belongs beside."""

    def __init__(self, field: str, message: str, *, existing: str = "") -> None:
        super().__init__(message)
        self.field = field
        self.message = message
        self.existing = existing

    def body(self) -> dict[str, Any]:
        return {"refused": {"field": self.field, "message": self.message, "id": self.existing}}


def display_address(base_url: str) -> str:
    """The address the way the owner typed it: no scheme and no trailing `/v1`."""
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    shown = parts.netloc + path
    return shown if parts.scheme == "http" else f"{parts.scheme}://{shown}"


def address_key(base_url: str) -> str:
    """Two spellings of one server compare equal on this."""
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return (parts.netloc + path).casefold()


def _on_own_network(host: str) -> bool:
    """True for an address that is plainly a machine on the owner's own network."""
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host.strip("[]")
    try:
        found = ipaddress.ip_address(name)
    except ValueError:
        return "." not in name or name.endswith((".local", ".lan", ".home.arpa", ".internal"))
    return found.is_private or found.is_loopback or found.is_link_local


def candidates(address: str) -> list[str]:
    """Every base address worth trying for what the owner typed, most likely first."""
    text = address.strip().rstrip("/")
    if not text or any(c.isspace() for c in text):
        return []
    schemes = ["http", "https"]
    if "://" in text:
        scheme, _, text = text.partition("://")
        if scheme.lower() not in {"http", "https"}:
            return []
        schemes = [scheme.lower()]
    host, slash, path = text.partition("/")
    if not host or "@" in host or "?" in text or "#" in text:
        return []
    if len(schemes) == 2 and (host.endswith(":443") or not _on_own_network(host)):
        schemes = ["https"]
    path = (slash + path).rstrip("/")
    paths = [path] if path.endswith("/v1") else [path + "/v1", path]
    return [f"{scheme}://{host}{tail}" for scheme in schemes for tail in paths]


def is_outside(base_url: str) -> bool:
    """True when the address is not on a private network, so what is sent leaves it."""
    host = urlsplit(base_url).hostname or ""
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            ]
        except OSError:
            # A name that does not resolve from here is a name on the owner's own network
            # at best; nothing is sent to it until it does resolve.
            return False
    return not all(a.is_private or a.is_loopback or a.is_link_local for a in addresses)


def http_status(error: ProviderError) -> int:
    """The HTTP status inside a provider error, or 0 when it was not an HTTP refusal."""
    match = re.search(r"HTTP (\d{3})", str(error)) or re.fullmatch(
        r"transcription-http-(\d{3})", error.rule
    )
    return int(match.group(1)) if match else 0


def speech_failure(error: ProviderError) -> str:
    """Why the recording test failed, in words that say what was observed."""
    rule = error.rule
    status = http_status(error)
    if rule in {"transcript-flat", "transcript-no-speakers"}:
        return "It answered, but did not say who was speaking."
    if rule in {"transcript-empty", "transcription-no_speech"}:
        return "It answered, but heard no speech in the sample."
    if rule in {"provider-no-response", "provider-too-long", "provider-stalled"}:
        return f"It was too slow: no answer to a 10-second sample within {SPEECH_WAIT_S} seconds."
    if rule == "transcription-too_long" or status == 413:
        return "It refused the 10-second sample as too large."
    if rule == "transcription-audio_unreadable" or status in {400, 415, 422}:
        return "It refused the sample file."
    if status in {401, 403}:
        return "It refused the key."
    if status in {404, 405} or rule == "transcription-provider":
        return "It does not accept recordings."
    return "It did not write up the sample recording."


class AiServices:
    def __init__(self, context: ContextService) -> None:
        self.context = context
        context.provider.observer = self.observe
        self._resuming: set[asyncio.Task[Any]] = set()
        self._testing: Counter[str] = Counter()

    # -- what is recorded -----------------------------------------------------------------

    def records(self) -> dict[str, dict[str, Any]]:
        return self.context.read(RECORD, {})

    def record(self, connection: ModelConnection) -> dict[str, Any]:
        return self.records().get(self.context.storage_identity(connection.id), {})

    def _update(self, connection_id: str, **changes: Any) -> dict[str, Any]:
        records = self.records()
        key = self.context.storage_identity(connection_id)
        record = {**records.get(key, {}), **changes}
        # The address a service had before a change is only worth saying while nothing
        # answers at the new one. Once it answers, that is forgotten.
        if changes.get("down_since") == "" and "address_before" not in changes:
            record.pop("address_before", None)
        records[key] = record
        self.context.write(RECORD, records)
        return record

    def _without_key(self, connection: ModelConnection, text: str) -> str:
        """What a service said when it failed, with its key taken out if it repeated it."""
        with contextlib.suppress(Exception):
            key = self.context.key(connection.id)
            if key:
                text = text.replace(key, "[key]")
                if len(key) > 12:
                    # Some services quote the key with its middle starred out.
                    text = text.replace(key[:8], "[key]").replace(key[-8:], "[key]")
        return text[:500]

    def observe(self, connection: ModelConnection, error: ProviderError | None) -> None:
        """One provider call's outcome. Called for every call, so it writes only on a change."""
        try:
            known = {item.id for item in self.context.config().connections}
            if connection.id not in known:
                return
            record = self.record(connection)
            down = bool(record.get("down_since"))
            refused = error is not None and http_status(error) in {401, 403}
            if error is not None and (error.rule in UNREACHABLE or refused):
                self._update(
                    connection.id,
                    down_since=record.get("down_since") or now(),
                    fails=int(record.get("fails", 0)) + 1,
                    last_failed_at=now(),
                    detail=self._without_key(connection, str(error)),
                    key_refused=refused,
                )
            elif down:
                self._update(
                    connection.id,
                    down_since="",
                    fails=0,
                    detail="",
                    key_refused=False,
                    last_ok_at=now(),
                )
                # It answered again without anyone pressing Test: send what was waiting on it.
                # A test in progress does this itself, so that it can say what it restarted.
                with contextlib.suppress(RuntimeError):
                    if self._testing[connection.id]:
                        return
                    task = asyncio.get_running_loop().create_task(self.resume(connection.id))
                    self._resuming.add(task)
                    task.add_done_callback(self._resuming.discard)
        except (OSError, ValueError):
            # Health is a convenience for the owner. It never fails the call it watched.
            return

    # -- views ---------------------------------------------------------------------------

    def used_by(self, connection_id: str) -> list[str]:
        config = self.context.config()
        used = []
        enrichment = config.assignments["enrichment"]
        transcription = config.assignments.get("transcription")
        if enrichment.enabled and enrichment.connection == connection_id:
            used.append("sugg")
        if transcription and transcription.enabled and transcription.connection == connection_id:
            used.append("trans")
        if config.embeddings.enabled and config.embeddings.connection == connection_id:
            used.append("search")
        return used

    def view(self, connection: ModelConnection) -> dict[str, Any]:
        record = self.record(connection)
        caps = record.get("caps") or {}
        return {
            "id": connection.id,
            "kind": KINDS[connection.provider],
            "name": connection.name,
            "address": display_address(connection.base_url) if connection.base_url else "",
            "outside": bool(record.get("outside", connection.provider != "compatible")),
            "has_key": bool(self.context.key(connection.id)),
            "answering": not record.get("down_since"),
            "down_since": record.get("down_since", ""),
            # Set while a changed address has not answered yet, so the page can name the
            # address to go back to instead of guessing at a sleeping computer.
            "address_before": record.get("address_before", "") if record.get("down_since") else "",
            "key_refused": bool(record.get("key_refused")),
            "fails": int(record.get("fails", 0)),
            "last_failed_at": record.get("last_failed_at", ""),
            "detail": record.get("detail", ""),
            "tested_at": record.get("tested_at", ""),
            "text": caps.get("text") or {"ok": False, "models": [], "best": ""},
            "speech": caps.get("speech") or {"ok": False, "models": [], "reason": ""},
            "embed": caps.get("embed") or {"ok": False, "model": ""},
            "used_by": self.used_by(connection.id),
        }

    def listing(self) -> list[dict[str, Any]]:
        return [self.view(item) for item in self.context.config().connections]

    def get(self, identity: str) -> dict[str, Any]:
        return self.view(self.context.connection(identity))

    # -- the one test --------------------------------------------------------------------

    def _text_models(self, connection: ModelConnection, models: list[str]) -> list[str]:
        chat = [m for m in models if not SPEECH_NAMES.search(m) and not EMBED_NAMES.search(m)]
        if connection.provider == "compatible":
            return chat[:OWN_MODEL_LIMIT]
        if connection.provider == "anthropic":
            families = ("haiku", "sonnet")
        else:
            chat = [
                m
                for m in chat
                if m.startswith("gpt-")
                and not re.search(r"audio|realtime|tts|search|image|instruct|codex|\d{4}-\d\d", m)
            ]
            families = ("mini", "")
        picked: list[str] = []
        for family in families:
            pool = (
                [m for m in chat if family in m]
                if family
                else [m for m in chat if "mini" not in m and "nano" not in m]
            )
            if pool:
                picked.append(max(pool))
        return picked[:OUTSIDE_MODEL_LIMIT]

    async def _test_text(
        self, connection: ModelConnection, key: str, models: list[str]
    ) -> dict[str, Any]:
        from coppermind_store.context_service import SAMPLE_MEETING

        results: list[dict[str, Any]] = []
        # A service with no model list is still asked once, so "it did not answer a request
        # for text" is something that was seen and not something assumed.
        for model in self._text_models(connection, models) or ([] if models else [""]):
            started = time.monotonic()
            assignment = FeatureAssignment(
                connection=connection.id,
                model=model,
                timeout_s=TEST_SILENCE_S,
                max_output_tokens=1024,
            )
            try:
                output, usage = await self.context.provider.suggest(
                    connection,
                    key,
                    assignment,
                    "Return only a JSON object with two keys: ok, set to true, and summary, a "
                    "plain paragraph of about 60 words that summarizes the supplied meeting text.",
                    {"synthetic_connection_test": True, "meeting_text": SAMPLE_MEETING},
                )
            except ProviderError as exc:
                if exc.rule in UNREACHABLE:
                    raise
                if model:
                    results.append({"name": model, "ok": False, "secs": 0, "sample": ""})
                continue
            passed = output.get("ok") is True
            results.append(
                {
                    "name": model,
                    "ok": passed,
                    "secs": max(1, round(time.monotonic() - started)),
                    "sample": str(output.get("summary", ""))[:400] if passed else "",
                    "speed": self.context.measured_speed(usage) if passed else {},
                }
            )
        working = sorted((r for r in results if r["ok"]), key=lambda r: r["secs"])
        return {
            "ok": bool(working),
            "models": results,
            # The fastest model that passed is the one recommended.
            "best": working[0]["name"] if working else "",
        }

    async def _test_embed(
        self, connection: ModelConnection, key: str, models: list[str], text: dict[str, Any]
    ) -> dict[str, Any]:
        if connection.provider == "anthropic":
            return {"ok": False, "model": ""}
        named = [m for m in models if EMBED_NAMES.search(m)]
        if connection.provider == "openai":
            named = sorted(named, key=lambda m: ("small" not in m, m))[:1]
        failed_chat = [r["name"] for r in text["models"] if not r["ok"] and r["name"]]
        for model in (named + [m for m in failed_chat if m not in named])[:3]:
            try:
                await self.context.provider.embed(
                    connection, key, model, ["Coppermind connection test"], timeout=TEST_SILENCE_S
                )
            except ProviderError as exc:
                if exc.rule in UNREACHABLE:
                    raise
                continue
            return {"ok": True, "model": model}
        return {"ok": False, "model": ""}

    async def _test_speech(
        self, connection: ModelConnection, key: str, models: list[str]
    ) -> dict[str, Any]:
        if connection.provider == "anthropic":
            return {
                "ok": False,
                "models": [],
                "reason": "Anthropic does not accept recordings.",
            }
        named = [m for m in models if SPEECH_NAMES.search(m)]
        if connection.provider == "openai":
            named = [m for m in named if "diari" in m][:1]
            if not named:
                return {
                    "ok": False,
                    "models": [],
                    "reason": "This account has no model that tells speakers apart.",
                }
        tries = named[:3] or [""]
        size = SAMPLE_AUDIO.stat().st_size
        reason = ""
        found: list[dict[str, Any]] = []
        for model in tries:
            # The reply format is found by trying one, then the other, and keeping what works.
            for reply_format in REPLY_FORMATS:
                assignment = FeatureAssignment(
                    connection=connection.id,
                    model=model,
                    wait_s=SPEECH_WAIT_S,
                    response_format=reply_format,
                )
                started = time.monotonic()
                try:
                    transcript, _ = await self.context.provider.transcribe(
                        connection,
                        key,
                        assignment,
                        SAMPLE_AUDIO,
                        filename=SAMPLE_AUDIO.name,
                        mime_type="audio/mpeg",
                        size_bytes=size,
                    )
                except ProviderError as exc:
                    if exc.rule in UNREACHABLE:
                        raise
                    reason = reason or speech_failure(exc)
                    if http_status(exc) in {401, 403, 404, 405}:
                        break
                    continue
                speakers = list(dict.fromkeys(s["speaker"] for s in transcript["segments"]))
                names = {name: f"Speaker {index + 1}" for index, name in enumerate(speakers)}
                # One line for each speaker it told apart: the first thing each one said.
                first: dict[str, dict[str, Any]] = {}
                for segment in transcript["segments"]:
                    first.setdefault(segment["speaker"], segment)
                found.append(
                    {
                        "model": model,
                        "format": reply_format,
                        "secs": max(1, round(time.monotonic() - started)),
                        "speakers": len(speakers),
                        "lines": [
                            {"speaker": names[s["speaker"]], "text": s["text"][:200]}
                            for s in list(first.values())[:4]
                        ],
                    }
                )
                break
        if not found:
            return {"ok": False, "models": [], "reason": reason}
        first = found[0]
        return {
            "ok": True,
            "models": [item["model"] for item in found if item["model"]],
            "formats": {item["model"]: item["format"] for item in found},
            "model": first["model"],
            "format": first["format"],
            "secs": first["secs"],
            "speakers": first["speakers"],
            "lines": first["lines"],
            "reason": "",
        }

    async def _run(self, connection: ModelConnection, key: str) -> dict[str, Any]:
        """Find out what one service can do. Raises when nothing answers or the key fails."""
        self._testing[connection.id] += 1
        try:
            try:
                models = await self.context.provider.models(connection, key)
            except ProviderError as exc:
                if exc.rule in UNREACHABLE or http_status(exc) in {401, 403}:
                    raise
                # No model list is an answer in itself: a transcription server often has none.
                models = []
            text = await self._test_text(connection, key, models)
            embed = await self._test_embed(connection, key, models, text)
            speech = await self._test_speech(connection, key, models)
            return {"models": models, "text": text, "embed": embed, "speech": speech}
        finally:
            self._testing[connection.id] -= 1

    def _stamp(self, connection: ModelConnection, caps: dict[str, Any]) -> None:
        """Keep the older records the workers read in step with what the test found."""
        from coppermind_store.context_service import fingerprint

        storage = self.context.storage_identity(connection.id)
        discovery = self.context.read("discovery", {})
        discovery[storage] = caps.get("models", [])
        self.context.write("discovery", discovery)
        tests = self.context.read("model_tests", {})
        speeds = self.context.read("model_speed", {})
        for result in caps.get("text", {}).get("models", []):
            if not result["ok"]:
                continue
            token = fingerprint(
                {
                    "connection": self.context.connection_evidence(connection),
                    "model": result["name"],
                }
            )
            tests[token] = now()
            if result.get("speed"):
                speeds[f"{connection.id}/{result['name']}"] = result["speed"] | {"tested_at": now()}
        self.context.write("model_tests", tests)
        self.context.write("model_speed", speeds)

    async def test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Test one service now, record what it can do, and restart what was waiting on it."""
        connection = self.context.connection(str(payload["id"]))
        key = self.context.key(connection.id)
        try:
            caps = await self._run(connection, key)
        except ProviderError as exc:
            refused = http_status(exc) in {401, 403}
            async with self.context.lock:
                record = self.record(connection)
                self._update(
                    connection.id,
                    down_since=record.get("down_since") or now(),
                    last_failed_at=now(),
                    fails=int(record.get("fails", 0)) + 1,
                    detail=self._without_key(connection, str(exc)),
                    key_refused=refused,
                )
            return {"answering": False, "key_refused": refused, "service": self.get(connection.id)}
        async with self.context.lock:
            # The connection may have been renamed while the test ran; stamp what is saved now.
            connection = self.context.connection(connection.id)
            self._update(
                connection.id,
                caps=caps,
                tested_at=now(),
                down_since="",
                fails=0,
                detail="",
                key_refused=False,
                last_ok_at=now(),
                outside=connection.provider != "compatible"
                or await asyncio.to_thread(is_outside, connection.base_url),
            )
            self._stamp(connection, caps)
        resumed = await self.resume(connection.id)
        return {"answering": True, "service": self.get(connection.id), "resumed": resumed}

    async def resume(self, connection_id: str) -> dict[str, int]:
        """Queue again whatever stopped because this service was not answering."""
        config = self.context.config()
        counts = {"notes": 0, "recordings": 0}
        enrichment = config.assignments["enrichment"]
        if enrichment.enabled and enrichment.connection == connection_id:
            latest: dict[str, Any] = {}
            for job in sorted(self.context.jobs(), key=lambda item: item.created_at):
                latest[job.note_id] = job
            for job in latest.values():
                if job.state == "failed" and job.error_rule in UNREACHABLE:
                    try:
                        await self.context.retry_job({"id": job.id})
                        counts["notes"] += 1
                    except Exception:  # noqa: BLE001 - one note that cannot be retried is not news
                        continue
        transcription = config.assignments.get("transcription")
        if transcription and transcription.enabled and transcription.connection == connection_id:
            for recording in self.context.recordings.jobs():
                waiting = recording.state == "queued" and recording.retry_after
                failed = recording.state == "failed" and recording.error_rule in UNREACHABLE
                if not (waiting or failed):
                    continue
                async with self.context.lock:
                    current = self.context.recordings.load(recording.id)
                    held = current.state == "failed" and current.error_rule in UNREACHABLE
                    if not (held or (current.state == "queued" and current.retry_after)):
                        continue
                    current.state, current.attempts, current.busy_since = "queued", 0, ""
                    current.set_error()
                    self.context.recordings.save(current)
                counts["recordings"] += 1
        return counts

    # -- add, edit, remove ----------------------------------------------------------------

    def _fresh_name(self) -> str:
        taken = {item.name for item in self.context.config().connections}
        name, index = "My AI server", 2
        while name in taken:
            name, index = f"My AI server {index}", index + 1
        return name

    def suggest_name(self) -> str:
        return self._fresh_name()

    @staticmethod
    def _refuse_duplicate(
        config: Any, kind: str, provider: str, bases: list[str], name: str
    ) -> None:
        if kind != "own":
            same = next((c for c in config.connections if c.provider == provider), None)
            if same:
                raise Refused(
                    "key",
                    f'That account is already in the list as "{same.name}". '
                    "A service is entered once.",
                    existing=same.id,
                )
            return
        same = next(
            (
                c
                for c in config.connections
                if c.provider == "compatible" and address_key(c.base_url) == address_key(bases[0])
            ),
            None,
        )
        if same:
            raise Refused(
                "address",
                f'That server is already in the list as "{same.name}". A service is entered once.',
                existing=same.id,
            )
        if any(c.name == name for c in config.connections):
            raise Refused(
                "name",
                f'You already have a service called "{name}". Give this one a different '
                "name so you can tell them apart.",
            )

    async def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Enter a service once: find it, test it, and only then keep it."""
        kind = str(payload.get("kind", "own"))
        key = str(payload.get("key", "")).strip()
        if kind not in PROVIDERS or len(key) > 4096:
            raise ValueError("unknown kind of service")
        provider = PROVIDERS[kind]
        if kind != "own":
            if not key:
                raise Refused("key", "Paste your API key.")
            name = "OpenAI" if kind == "openai" else "Anthropic"
            bases = [""]
        else:
            bases = candidates(str(payload.get("address", "")))
            if not bases:
                raise Refused(
                    "address", "Type the address of your server, for example 192.168.1.20:8000"
                )
            name = str(payload.get("name", "")).strip() or display_address(bases[0])
        self._refuse_duplicate(self.context.config(), kind, provider, bases, name)
        identity = "conn_" + new_id().lower()
        caps: dict[str, Any] | None = None
        chosen: ModelConnection | None = None
        unreachable = True
        for base in bases:
            try:
                connection = ModelConnection(
                    id=identity,
                    name=name[:120],
                    provider=provider,  # type: ignore[arg-type]
                    base_url=base,
                )
            except ValueError as exc:
                raise Refused(
                    "address", "Type the address of your server, for example 192.168.1.20:8000"
                ) from exc
            try:
                found = await self._run(connection, key)
            except ProviderError as exc:
                status = http_status(exc)
                if status in {401, 403}:
                    raise Refused(
                        "key",
                        "The service refused that key. Check it and try again."
                        if key
                        else "This server asked for a key. Type it and connect again.",
                    ) from exc
                unreachable = unreachable and exc.rule in {"provider-connect"}
                continue
            unreachable = False
            able = found["text"]["ok"] or found["speech"]["ok"] or found["embed"]["ok"]
            if caps is None or able:
                caps, chosen = found, connection
            if able:
                break
        if caps is None or chosen is None:
            shown = str(payload.get("address", "")).strip()
            raise Refused(
                "address" if kind == "own" else "key",
                (
                    f"Nothing answers at {shown}. Check the address, and that the server is "
                    "switched on, then try again. A server on your own network that does not "
                    "use https needs http:// in front of its address."
                    if unreachable
                    else f"Something answers at {shown}, but not the way an AI service does. "
                    "Check the address and try again."
                )
                if kind == "own"
                else f"{name} did not answer. Check the key and your internet connection.",
            )
        outside = kind != "own" or await asyncio.to_thread(is_outside, chosen.base_url)
        async with self.context.lock:
            config = self.context.config()
            self._refuse_duplicate(config, kind, provider, bases, name)
            chosen.credential_version = 1
            config.connections.append(chosen)
            config.automatic_connection_ids.append(chosen.id)
            credentials = self.context.read("credentials", {})
            credentials[chosen.id] = key
            self.context.write("credentials", credentials)
            self.context.save_config(config)
            self._update(
                chosen.id,
                caps=caps,
                tested_at=now(),
                down_since="",
                fails=0,
                detail="",
                key_refused=False,
                last_ok_at=now(),
                outside=outside,
            )
            self._stamp(chosen, caps)
        return {"service": self.get(chosen.id)}

    async def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Rename a service, or move it to a new address or key and test it there at once."""
        identity = str(payload["id"])
        name = str(payload.get("name", "")).strip()
        new_key = str(payload.get("key", ""))
        if not name:
            raise Refused("name", "Give it a name.")
        async with self.context.lock:
            config = self.context.config()
            connection = next((c for c in config.connections if c.id == identity), None)
            if connection is None:
                raise ValueError("That service is no longer in the list.")
            if any(c.name == name and c.id != identity for c in config.connections):
                raise Refused("name", "Another service already has that name.")
            moved_to: list[str] = []
            if connection.provider == "compatible":
                typed = str(payload.get("address", "")).strip()
                bases = candidates(typed)
                if not bases:
                    raise Refused("address", "Type the address of the server.")
                # A scheme the owner typed is theirs to choose: http changed to https (or back)
                # is a move even though the server is the same one, so it is saved and tested.
                typed_scheme = typed.partition("://")[0].lower() if "://" in typed else ""
                stored_scheme = urlsplit(connection.base_url).scheme.lower()
                if address_key(bases[0]) == address_key(connection.base_url):
                    if typed_scheme and typed_scheme != stored_scheme:
                        moved_to = bases
                else:
                    clash = next(
                        (
                            c
                            for c in config.connections
                            if c.id != identity
                            and c.provider == "compatible"
                            and address_key(c.base_url) == address_key(bases[0])
                        ),
                        None,
                    )
                    if clash:
                        raise Refused(
                            "address",
                            f'That server is already in the list as "{clash.name}".',
                            existing=clash.id,
                        )
                    moved_to = bases
            renamed = connection.name != name
            connection.name = name[:120]
            if not moved_to and not new_key:
                self.context.save_config(config)
                caps = self.record(connection).get("caps")
                if caps:
                    self._stamp(connection, caps)
                return {"service": self.get(identity), "renamed": renamed, "tested": False}
        key = new_key or self.context.key(identity)
        caps = None
        chosen = connection
        error: ProviderError | None = None
        for base in moved_to or [connection.base_url]:
            candidate = connection.model_copy(update={"base_url": base})
            try:
                found = await self._run(candidate, key)
            except ProviderError as exc:
                error = error or exc
                continue
            able = found["text"]["ok"] or found["speech"]["ok"] or found["embed"]["ok"]
            if caps is None or able:
                caps, chosen = found, candidate
            if able:
                break
        async with self.context.lock:
            config = self.context.config()
            current = next((c for c in config.connections if c.id == identity), None)
            if current is None:
                raise ValueError("That service is no longer in the list.")
            current.name = name[:120]
            if moved_to:
                current.base_url = chosen.base_url if caps else moved_to[0]
            current.credential_version += 1
            if new_key:
                credentials = self.context.read("credentials", {})
                credentials[self.context.storage_identity(identity)] = new_key
                self.context.write("credentials", credentials)
            self.context.save_config(config)
            if caps is None:
                refused = error is not None and http_status(error) in {401, 403}
                moved = {}
                if moved_to:
                    # Two slips in a row still point back at the address that last worked.
                    moved["address_before"] = self.record(connection).get(
                        "address_before"
                    ) or display_address(connection.base_url)
                self._update(
                    identity,
                    **moved,
                    down_since=now(),
                    last_failed_at=now(),
                    fails=1,
                    detail=self._without_key(connection, str(error)) if error else "",
                    key_refused=refused,
                    outside=current.provider != "compatible"
                    or await asyncio.to_thread(is_outside, current.base_url),
                )
                return {
                    "service": self.get(identity),
                    "tested": True,
                    "answering": False,
                    "moved": bool(moved_to),
                    "key_refused": refused,
                }
            self._update(
                identity,
                caps=caps,
                tested_at=now(),
                down_since="",
                fails=0,
                detail="",
                key_refused=False,
                last_ok_at=now(),
                outside=current.provider != "compatible"
                or await asyncio.to_thread(is_outside, current.base_url),
            )
            self._stamp(current, caps)
        resumed = await self.resume(identity)
        return {
            "service": self.get(identity),
            "tested": True,
            "answering": True,
            "moved": bool(moved_to),
            "resumed": resumed,
        }

    def forget(self, config: Any, identity: str) -> None:
        """Drop one connection and everything recorded about it. The caller saves `config`."""
        storage = self.context.storage_identity(identity)
        config.connections = [c for c in config.connections if c.id != identity]
        config.connection_aliases.pop(identity, None)
        config.automatic_connection_ids = [
            item for item in config.automatic_connection_ids if item != identity
        ]
        for name in ("credentials", "discovery", RECORD):
            values = self.context.read(name, {})
            if storage in values:
                del values[storage]
                self.context.write(name, values)
