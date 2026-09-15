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
    ApiKeyRecord,
    ApiKeySet,
    create_key,
    split_credential,
    verify_secret,
)
from coppermind.atomicio import atomic_write_text
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict
from coppermind_store.control import ControlState

DEFAULT_KEY_NAME = "bootstrap default"
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


def _reveals(secret_file: Path, record: ApiKeyRecord) -> bool:
    """Say whether the reveal file still holds the credential for `record`."""
    if not secret_file.exists() or secret_file.stat().st_size == 0:
        return False
    parsed = split_credential(secret_file.read_text(encoding="utf-8").strip())
    if parsed is None:
        return False
    key_id, secret = parsed
    return key_id == record.key_id and verify_secret(record.hash, secret)


def ensure_default_key(control: ControlState, secret_file: Path) -> str:
    """Ensure bootstrap's full-scope default key, saying what became of it.

    `keys.json` decides, not the reveal file, because the control state is what
    a backup carries and what revocation is recorded in. A revoked default
    stays revoked however the credential volume was lost: the reveal file is
    replaced by a sentence saying so, and no replacement is minted. A live
    default whose credential is still readable is kept untouched. A live
    default whose credential is gone is re-issued, which rotates only a
    credential nobody could use, and a default that was never recorded is
    generated.
    """
    key_set, exists = _load_keys(control)
    current = next(
        (record for record in reversed(key_set.keys) if record.name == DEFAULT_KEY_NAME), None
    )
    if current is not None:
        if current.revoked_at is not None:
            atomic_write_text(secret_file, REVOKED_NOTICE + "\n", mode=0o600)
            return "revoked"
        if _reveals(secret_file, current):
            return "kept"

    record, credential = create_key(DEFAULT_KEY_NAME, list(API_SCOPES))
    atomic_write_text(secret_file, credential + "\n", mode=0o600)
    key_set.keys.append(record)
    control.store.write(
        "keys",
        key_set.model_dump(mode="json"),
        if_revision=key_set.revision if exists else None,
    )
    return "re-issued" if current is not None else "generated"


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
