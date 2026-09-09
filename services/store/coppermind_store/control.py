"""Control state as the store sees it.

The store owns `/data/state`. On boot it makes sure every control file exists
with its shipped defaults, so a fresh install has working settings and a
working frontmatter schema without anyone populating anything. Reads go
straight to the file, which is small, and always reflect what Admin last
wrote.
"""

from __future__ import annotations

from pathlib import Path

from coppermind.schema import FrontmatterSchema, default_schema
from coppermind.settings import ProductSettings, default_settings
from coppermind.statefiles import StateStore


class ControlState:
    def __init__(self, state_dir: Path) -> None:
        self.store = StateStore(state_dir)

    def ensure_defaults(self) -> None:
        """Create any missing control file at revision 1 with shipped defaults."""
        self.store.state_dir.mkdir(parents=True, exist_ok=True)
        self.store.ensure("settings", default_settings().model_dump(mode="json"))
        self.store.ensure("schema", default_schema().model_dump(mode="json"))

    def settings(self) -> ProductSettings:
        body = dict(self.store.read("settings").body)
        body.pop("revision", None)
        return ProductSettings.model_validate(body)

    def schema(self) -> FrontmatterSchema:
        body = dict(self.store.read("schema").body)
        body.pop("revision", None)
        return FrontmatterSchema.model_validate(body)
