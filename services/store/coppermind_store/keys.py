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
from coppermind_store.control import ControlState


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


def ensure_default_key(control: ControlState, secret_file: Path) -> bool:
    """Ensure bootstrap's full-scope default key and reveal file agree.

    The reveal file is written before its hash record. If bootstrap is
    interrupted between those writes, the next run repairs ``keys.json`` from
    the credential instead of rotating it or leaving a fresh install unusable.
    """
    credential = ""
    if secret_file.exists() and secret_file.stat().st_size > 0:
        credential = secret_file.read_text(encoding="utf-8").strip()
        parsed = split_credential(credential)
        if parsed is None:
            raise RuntimeError(f"the default API key file at {secret_file} is malformed")
        key_id, secret = parsed
        key_set, exists = _load_keys(control)
        current = next((record for record in key_set.keys if record.key_id == key_id), None)
        if current is not None:
            if current.revoked_at is not None:
                return False
            if not verify_secret(current.hash, secret):
                raise RuntimeError("the default API key file does not match its key record")
            return False
        record, _ = create_key(
            "bootstrap default",
            list(API_SCOPES),
            key_id=key_id,
            secret=secret,
        )
    else:
        key_set, exists = _load_keys(control)
        record, credential = create_key("bootstrap default", list(API_SCOPES))
        atomic_write_text(secret_file, credential + "\n", mode=0o600)

    key_set.keys.append(record)
    control.store.write(
        "keys",
        key_set.model_dump(mode="json"),
        if_revision=key_set.revision if exists else None,
    )
    return True


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
    if args.command == "create":
        credential = add_key(control, args.name, args.scopes or list(API_SCOPES))
        print(credential)
    elif args.command == "revoke":
        revoke_key(control, args.key_id)
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
