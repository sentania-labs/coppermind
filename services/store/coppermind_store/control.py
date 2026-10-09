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
from coppermind.settings import ProductSettings, default_settings, read_settings
from coppermind.statefiles import StateStore
from coppermind.store_protocol import SchemaDocument


class ControlState:
    def __init__(self, state_dir: Path) -> None:
        self.store = StateStore(state_dir)

    def ensure_defaults(self) -> None:
        """Create any missing control file at revision 1 with shipped defaults."""
        self.store.state_dir.mkdir(parents=True, exist_ok=True)
        self.store.ensure("settings", default_settings().model_dump(mode="json"))
        self.store.ensure("schema", default_schema().model_dump(mode="json"))

    def settings(self) -> ProductSettings:
        return read_settings(self.store)

    def schema(self) -> FrontmatterSchema:
        return self.schema_document().frontmatter_schema

    def schema_document(self) -> SchemaDocument:
        """The schema and the revision of the file it came from, in one read."""
        state = self.store.read("schema")
        body = dict(state.body)
        body.pop("revision", None)
        return SchemaDocument(
            revision=state.revision, frontmatter_schema=FrontmatterSchema.model_validate(body)
        )

    def api_keys(self) -> ApiKeySet:
        return ApiKeySet.model_validate(self.store.read("keys").body)

    def write_reconciler_exclusions(self, exclusions: dict[str, str]) -> None:
        """Publish one atomic snapshot, so count and rules describe the same pass."""
        try:
            revision = self.store.read("reconciler_exclusions").revision
        except FileNotFoundError:
            revision = None
        self.store.write("reconciler_exclusions", {"files": exclusions}, if_revision=revision)

    def read_reconciler_exclusions(self) -> dict[str, str]:
        """The last completed scan's exclusions; empty before the first scan."""
        try:
            body = self.store.read("reconciler_exclusions").body
        except FileNotFoundError:
            return {}
        files = body.get("files", {})
        if not isinstance(files, dict) or any(
            not isinstance(path, str) or not isinstance(rule, str) for path, rule in files.items()
        ):
            raise ValueError("invalid reconciler exclusion snapshot")
        return files

    def read_reconciler_excluded_count(self) -> int:
        return len(self.read_reconciler_exclusions())
