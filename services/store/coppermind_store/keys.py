"""Create API keys in filesystem-first control state.

The command is the interim rotation path until the separate Admin service
ships its graphical keys page. It prints the credential once. ``keys.json``
receives only the Argon2 hash.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from coppermind.api_keys import (
    API_SCOPES,
    ApiKeySet,
    create_key,
    split_credential,
    verify_secret,
)
from coppermind.atomicio import atomic_write_text
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict
from coppermind_store.control import ControlState

REVOKED_NOTICE = (
    "the default API key was revoked; create a new key with: "
    "python3 -m coppermind_store.keys create"
)


def _load_keys(control: ControlState) -> tuple[ApiKeySet, bool]:
    path = control.store.path_for("keys")
    if not path.exists():
        return ApiKeySet(), False
    return control.api_keys(), True


def add_key(control: ControlState, name: str, scopes: list[str]) -> str:
    """Append a key and return the credential that must be shown once."""
    key_set, exists = _load_keys(control)
    record, credential = create_key(name, scopes)
    key_set.keys.append(record)
    control.store.write(
        "keys",
        key_set.model_dump(mode="json"),
        if_revision=key_set.revision if exists else None,
    )
    return credential


def revoke_key(control: ControlState, key_id: str) -> None:
    """Revoke an existing key without removing its audit record."""
    key_set, exists = _load_keys(control)
    record = next((candidate for candidate in key_set.keys if candidate.key_id == key_id), None)
    if not exists or record is None:
        raise ValueError(f"no API key with id {key_id}")
    if record.revoked_at is None:
        record.revoked_at = datetime.now(tz=UTC)
        control.store.write(
            "keys",
            key_set.model_dump(mode="json"),
            if_revision=key_set.revision,
        )


def ensure_default_key(control: ControlState, secret_file: Path) -> str:
    """Ensure bootstrap's full-scope default key, saying what became of it.

    The reveal file is kept whenever it names a live key record it verifies
    against, so a restart never rotates a credential in use. A revoked record
    stays revoked: the file is replaced by a sentence saying so, because an
    operator who reads it should be told rather than handed a credential that
    answers 401. Anything else, including a first start and a run interrupted
    between the reveal write and the record write, mints a fresh key and
    overwrites the file, which rotates only a credential nobody could use.
    """
    key_set, exists = _load_keys(control)
    if secret_file.exists() and secret_file.stat().st_size > 0:
        revealed = secret_file.read_text(encoding="utf-8").strip()
        if revealed == REVOKED_NOTICE:
            return "revoked"
        parsed = split_credential(revealed)
        if parsed is not None:
            key_id, secret = parsed
            current = next((record for record in key_set.keys if record.key_id == key_id), None)
            if current is not None and verify_secret(current.hash, secret):
                if current.revoked_at is None:
                    return "kept"
                atomic_write_text(secret_file, REVOKED_NOTICE + "\n", mode=0o600)
                return "revoked"

    record, credential = create_key("bootstrap default", list(API_SCOPES))
    atomic_write_text(secret_file, credential + "\n", mode=0o600)
    key_set.keys.append(record)
    control.store.write(
        "keys",
        key_set.model_dump(mode="json"),
        if_revision=key_set.revision if exists else None,
    )
    return "generated"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Manage Coppermind API keys")
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a key and print it once")
    create.add_argument("--name", default="command line")
    create.add_argument(
        "--scope",
        action="append",
        choices=API_SCOPES,
        dest="scopes",
        help="scope to grant; repeat as needed (default: every scope)",
    )
    revoke = commands.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("key_id")
    return result


def run(argv: list[str] | None = None, wiring: Wiring | None = None) -> int:
    args = parser().parse_args(argv)
    settings = wiring or Wiring()
    control = ControlState(settings.state_dir)
    try:
        if args.command == "create":
            credential = add_key(control, args.name, args.scopes or list(API_SCOPES))
            print(credential)
        elif args.command == "revoke":
            revoke_key(control, args.key_id)
    except (ValueError, RevisionConflict) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
