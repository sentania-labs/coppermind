"""First boot: make the volumes usable, then get out of the way.

This runs once per `docker compose up` as a one-shot container and exits 0. It
is the reason a clean checkout needs no hand populated setting and no
pre-created secret:

- `/data` gains its notes, sources and state trees, owned by uid 1000, which
  every Coppermind image runs as.
- `/run/coppermind` gains an internal bearer token and a PostgreSQL password,
  both generated here and never printed, never committed and never passed as
  an environment value.
- The control state files arrive at revision 1 with their shipped defaults, so
  Admin opens on working settings rather than an empty form.

Everything is idempotent. A second run leaves existing secrets and existing
settings exactly as they are, so restarting the stack never rotates a
credential or resets an operator's choices.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from pathlib import Path

from coppermind.atomicio import atomic_write_text
from coppermind.settings import Wiring
from coppermind_store.control import ControlState

TOKEN_BYTES = 32


def _own(path: Path, uid: int, gid: int, mode: int) -> None:
    """Set ownership and mode where the process is allowed to.

    The one-shot runs as root on compose so it can hand the volumes to uid
    1000. Run as an unprivileged user (a Kubernetes init container with a
    fsGroup already applied) the chown is not permitted and not needed, so it
    is skipped rather than treated as a failure.
    """
    with contextlib.suppress(PermissionError):
        os.chown(path, uid, gid)
    with contextlib.suppress(PermissionError):
        os.chmod(path, mode)


def _ensure_dir(path: Path, uid: int, gid: int, mode: int = 0o750) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _own(path, uid, gid, mode)


def _ensure_secret(path: Path, uid: int, gid: int, mode: int) -> bool:
    """Create `path` with a fresh random secret when it is missing.

    Returns True when a secret was generated, so the log can say what happened
    without ever showing the value.
    """
    if path.exists() and path.stat().st_size > 0:
        _own(path, uid, gid, mode)
        return False
    atomic_write_text(path, secrets.token_urlsafe(TOKEN_BYTES) + "\n", mode=mode)
    _own(path, uid, gid, mode)
    return True


def run(wiring: Wiring | None = None) -> int:
    settings = wiring or Wiring()
    uid, gid = settings.run_uid, settings.run_gid

    _ensure_dir(settings.data_dir, uid, gid, 0o755)
    _ensure_dir(settings.notes_dir, uid, gid, 0o755)
    _ensure_dir(settings.sources_dir, uid, gid, 0o755)
    _ensure_dir(settings.state_dir, uid, gid, 0o755)
    _ensure_dir(settings.state_dir / "history", uid, gid, 0o755)
    _ensure_dir(settings.state_dir / "internal", uid, gid, 0o700)
    # 0755 on the directory so the bundled PostgreSQL, which runs as a different
    # user, can traverse it to reach its own password file. The secrecy is in
    # the file modes below, not in the directory.
    _ensure_dir(settings.secrets_dir, uid, gid, 0o755)

    created_token = _ensure_secret(settings.internal_token_file, uid, gid, 0o600)
    # The bundled PostgreSQL image reads its password file as its own user
    # (uid 999), and the Coppermind services read it as uid 1000. Owner 999,
    # group 1000, mode 0640 lets exactly those two and nobody else.
    created_password = _ensure_secret(settings.db_password_file, settings.postgres_uid, gid, 0o640)

    control = ControlState(settings.state_dir)
    control.ensure_defaults()
    for name in ("settings", "schema"):
        _own(control.store.path_for(name), uid, gid, 0o644)

    print(
        "bootstrap complete: "
        f"data={settings.data_dir} secrets={settings.secrets_dir} "
        f"internal_token={'generated' if created_token else 'kept'} "
        f"postgres_password={'generated' if created_password else 'kept'}",
        flush=True,
    )
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
