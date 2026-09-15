"""Control state as the store sees it.

The store owns `/data/state`. On boot it makes sure settings and the
frontmatter schema exist with their shipped defaults, so a fresh install has
both without anyone populating anything. `keys.json` has no shipped default to
write: bootstrap mints the first key and creates the file. Reads go straight to
the file, which is small, and always reflect what Admin last wrote.
"""

from __future__ import annotations

from pathlib import Path

from coppermind.api_keys import ApiKeySet
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

    def api_keys(self) -> ApiKeySet:
        return ApiKeySet.model_validate(self.store.read("keys").body)
