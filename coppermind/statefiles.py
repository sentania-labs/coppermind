"""Control state files under `/data/state`.

Settings, the frontmatter schema, filing rules, API key hashes and the admin
record are files on the volume, not database rows. That is what makes a backup
of `/data` complete: restoring it restores the operator's configuration along
with the notes, and PostgreSQL can be dropped and rebuilt from it.

Every file carries `schema_version` and `revision`. A write states the revision
it is replacing, so two Admin tabs cannot silently overwrite each other, and
the previous revision is kept under `/data/state/history/` so a bad edit is
recoverable without a backup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML

from coppermind.atomicio import atomic_write_text

StateName = Literal["settings", "schema", "rules", "keys", "admin"]

# Extension per state file. JSON is used where the file holds hashes rather
# than operator prose, so nothing invites hand editing.
STATE_FILES: dict[str, str] = {
    "settings": "yaml",
    "schema": "yaml",
    "rules": "yaml",
    "keys": "json",
    "admin": "json",
}

HISTORY_KEEP = 50


class RevisionConflict(Exception):
    """The file moved on since the revision the caller read."""

    def __init__(self, current_revision: int) -> None:
        super().__init__(f"state file is at revision {current_revision}")
        self.current_revision = current_revision


class StateFile:
    """One control state file: its name, its revision and its body."""

    def __init__(self, name: str, revision: int, body: dict[str, Any]) -> None:
        self.name = name
        self.revision = revision
        self.body = body

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StateFile(name={self.name!r}, revision={self.revision})"


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


class StateStore:
    """Reads and writes the control state files of one `/data/state` directory."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.history_dir = state_dir / "history"

    def path_for(self, name: str) -> Path:
        try:
            extension = STATE_FILES[name]
        except KeyError as exc:
            raise ValueError(f"unknown state file {name!r}") from exc
        return self.state_dir / f"{name}.{extension}"

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def read(self, name: str) -> StateFile:
        """Read a state file. Raises FileNotFoundError when it was never written."""
        path = self.path_for(name)
        text = path.read_text(encoding="utf-8")
        loaded = _load(path, text)
        revision = int(loaded.get("revision", 1))
        return StateFile(name, revision, loaded)

    def write(self, name: str, body: dict[str, Any], *, if_revision: int | None) -> StateFile:
        """Replace a state file.

        `if_revision` is the revision the caller believes it is replacing.
        Pass None only when creating the file for the first time.
        """
        path = self.path_for(name)
        current = self.read(name) if path.exists() else None
        if current is not None:
            if if_revision is None or if_revision != current.revision:
                raise RevisionConflict(current.revision)
            self._archive(name, current, path)
        next_revision = 1 if current is None else current.revision + 1

        record = {"schema_version": int(body.get("schema_version", 1)), "revision": next_revision}
        record.update({k: v for k, v in body.items() if k not in {"schema_version", "revision"}})
        atomic_write_text(path, _dump(path, record), mode=_mode_for(name))
        self._trim_history(name)
        return StateFile(name, next_revision, record)

    def ensure(self, name: str, body: dict[str, Any]) -> StateFile:
        """Write `body` as revision 1 when the file does not exist yet.

        This is how a fresh install arrives with working defaults already in
        place instead of an empty state directory.
        """
        if self.path_for(name).exists():
            return self.read(name)
        return self.write(name, body, if_revision=None)

    def _archive(self, name: str, current: StateFile, path: Path) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        archived = self.history_dir / f"{name}.{current.revision}{path.suffix}"
        atomic_write_text(archived, path.read_text(encoding="utf-8"), mode=_mode_for(name))

    def _trim_history(self, name: str) -> None:
        if not self.history_dir.exists():
            return
        kept = sorted(
            self.history_dir.glob(f"{name}.*"),
            key=lambda p: _revision_of(p, name),
        )
        for stale in kept[:-HISTORY_KEEP]:
            stale.unlink(missing_ok=True)


def _revision_of(path: Path, name: str) -> int:
    try:
        return int(path.name[len(name) + 1 :].split(".")[0])
    except ValueError:
        return 0


def _mode_for(name: str) -> int:
    # keys.json and admin.json hold argon2 hashes. They are not usable as
    # credentials, but there is no reason for them to be world readable.
    return 0o600 if name in {"keys", "admin"} else 0o644


def _load(path: Path, text: str) -> dict[str, Any]:
    if path.suffix == ".json":
        import json

        loaded = json.loads(text)
    else:
        loaded = _yaml().load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} is not a mapping")
    return loaded


def _dump(path: Path, body: dict[str, Any]) -> str:
    if path.suffix == ".json":
        import json

        return json.dumps(body, indent=2, sort_keys=False) + "\n"
    import io

    stream = io.StringIO()
    _yaml().dump(body, stream)
    return stream.getvalue()
