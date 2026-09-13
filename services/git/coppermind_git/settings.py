"""The Git helper's view of `/data/state/settings.yaml`.

The helper reads its own `git` section and the two folder names it keeps out
of history, and nothing else. It does not validate the whole file against the
shared settings model: that model lives in a package that carries the database
drivers, and a store release that adds a settings section must never stop a
running helper. The defaults below are the shipped ones, and a test holds them
equal to `coppermind.settings`.

A missing file or key means the shipped default. A value the helper cannot use
raises `SettingsError`, and the caller keeps the last settings that worked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class SettingsError(ValueError):
    """settings.yaml holds a value the helper cannot use."""


@dataclass(frozen=True)
class HelperSettings:
    enabled: bool = True
    debounce_s: int = 60
    poll_interval_s: int = 300
    identity_name: str = "Coppermind"
    identity_email: str = "coppermind@localhost"
    gc_auto: bool = True
    sources_folder: str = "_Sources"
    trash_folder: str = "_Trash"


def load(path: Path) -> HelperSettings:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HelperSettings()
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(f"{path.name} could not be read: {exc}") from exc
    try:
        document = YAML(typ="safe").load(text)
    except YAMLError as exc:
        raise SettingsError(f"{path.name} is not valid YAML: {exc}") from exc
    if document is None:
        return HelperSettings()
    if not isinstance(document, dict):
        raise SettingsError(f"{path.name} is not a mapping")

    git = _section(document, "git")
    notes = _section(document, "notes")
    shipped = HelperSettings()
    return HelperSettings(
        enabled=_flag(git, "git.enabled", shipped.enabled),
        debounce_s=_seconds(git, "git.debounce_s", shipped.debounce_s),
        poll_interval_s=_seconds(git, "git.poll_interval_s", shipped.poll_interval_s),
        identity_name=_line(git, "git.identity_name", shipped.identity_name),
        identity_email=_line(git, "git.identity_email", shipped.identity_email),
        gc_auto=_flag(git, "git.gc_auto", shipped.gc_auto),
        sources_folder=_folder(notes, "notes.sources_folder", shipped.sources_folder),
        trash_folder=_folder(notes, "notes.trash_folder", shipped.trash_folder),
    )


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    section = document.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SettingsError(f"{name}: expected a mapping")
    return section


def _value(section: dict[str, Any], dotted: str, default: Any) -> Any:
    value = section.get(dotted.split(".", 1)[1])
    return default if value is None else value


def _flag(section: dict[str, Any], dotted: str, default: bool) -> bool:
    value = _value(section, dotted, default)
    if not isinstance(value, bool):
        raise SettingsError(f"{dotted}: expected true or false")
    return value


def _seconds(section: dict[str, Any], dotted: str, default: int) -> int:
    value = _value(section, dotted, default)
    # bool is an int in Python; `debounce_s: true` is a mistake, not 1 second.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SettingsError(f"{dotted}: expected a whole number of seconds, at least 1")
    return value


def _line(section: dict[str, Any], dotted: str, default: str) -> str:
    value = _value(section, dotted, default)
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise SettingsError(f"{dotted}: expected a non-empty single line of text")
    return value.strip()


def _folder(section: dict[str, Any], dotted: str, default: str) -> str:
    # An excluded folder is taken out of every snapshot, so a name that means
    # the whole notes filesystem would record every note as deleted.
    value = _line(section, dotted, default)
    parts = value.strip("/").split("/")
    if any(part.strip() in {"", ".", ".."} for part in parts):
        raise SettingsError(f"{dotted}: expected a folder inside the notes filesystem")
    return value.strip("/")
