"""Two kinds of settings, kept apart on purpose.

Product settings are the operator's: folder names, intervals, the sync plan,
limits. They live in `/data/state/settings.yaml`, they ship with the working
defaults below, and Admin edits them. Nothing has to be hand populated for a
fresh install to run.

Wiring is the deployer's: ports, hostnames, and the paths of the files that
hold credentials. Wiring comes from `COPPERMIND_` environment variables and
never from `settings.yaml`, because it differs between compose and Kubernetes
while the product settings do not. No credential is ever an environment value;
the environment names the file that holds one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024
GIB = 1024 * MIB

SyncPlan = Literal["standard", "plus"]

# File and total size ceilings Obsidian Sync enforces per plan. An explicit
# value in the sync section overrides the derived one.
PLAN_LIMITS: dict[str, tuple[int, int]] = {
    "standard": (5 * MIB, 1 * GIB),
    "plus": (200 * MIB, 10 * GIB),
}


class GeneralSettings(BaseModel):
    timezone: str = "America/Chicago"


class NotesSettings(BaseModel):
    """Folder layout of the notes filesystem. Every name is renameable."""

    review_folder: str = "Review"
    trash_folder: str = "_Trash"
    sources_folder: str = "_Sources"
    attachments_folder: str = "_Attachments"
    journal_folder: str = "Journal"
    dated_types: list[str] = Field(default_factory=lambda: ["meeting", "journal"])


class ReconcileSettings(BaseModel):
    scan_interval_s: int = 60
    quiet_period_s: int = 30
    full_rehash_daily_at: str = "03:30"


class GitSettings(BaseModel):
    enabled: bool = True
    debounce_s: int = 60
    poll_interval_s: int = 300
    identity_name: str = "Coppermind"
    identity_email: str = "coppermind@localhost"
    gc_auto: bool = True


class SyncSettings(BaseModel):
    plan: SyncPlan = "standard"
    # Null means "derive from the plan", which is what a fresh install does.
    max_file_bytes: int | None = None
    max_total_bytes: int | None = None
    device_name: str = "coppermind-server"
    mode: str = "bidirectional"
    conflict_strategy: str = "merge"
    excluded_folders: list[str] = Field(default_factory=lambda: ["_Trash"])
    file_types: list[str] = Field(default_factory=lambda: ["image", "audio", "video", "pdf"])
    sync_configs: list[str] = Field(default_factory=list)

    def effective_limits(self) -> tuple[int, int]:
        """Return (max file bytes, max total bytes) after applying the plan."""
        plan_file, plan_total = PLAN_LIMITS[self.plan]
        return (self.max_file_bytes or plan_file, self.max_total_bytes or plan_total)


class CuratorSettings(BaseModel):
    enabled: bool = True
    sweep_interval_s: int = 300
    inbox_only: bool = True


class IndexerSettings(BaseModel):
    enabled: bool = True
    language: str = "english"
    reconcile_interval_s: int = 600


class EventSettings(BaseModel):
    retention_days: int = 7
    poll_fallback_s: int = 5


class LimitSettings(BaseModel):
    ingest_max_bytes: int = 25 * MIB
    # Null means "the sync file ceiling", so an attachment is never accepted
    # that Obsidian Sync would then refuse to carry to a device.
    attachment_max_bytes: int | None = None


class AdminSettings(BaseModel):
    session_hours: int = 12


class ProductSettings(BaseModel):
    """The whole of `settings.yaml`, with the defaults a fresh install runs on."""

    schema_version: int = 1
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    notes: NotesSettings = Field(default_factory=NotesSettings)
    reconcile: ReconcileSettings = Field(default_factory=ReconcileSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    curator: CuratorSettings = Field(default_factory=CuratorSettings)
    indexer: IndexerSettings = Field(default_factory=IndexerSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)

    def attachment_limit_bytes(self) -> int:
        max_file, _ = self.sync.effective_limits()
        return self.limits.attachment_max_bytes or max_file


def default_settings() -> ProductSettings:
    """The settings a fresh install runs with."""
    return ProductSettings()


class Wiring(BaseSettings):
    """Deployment wiring. Every value has a default that works on compose."""

    model_config = SettingsConfigDict(env_prefix="COPPERMIND_", extra="ignore")

    data_dir: Path = Path("/data")
    secrets_dir: Path = Path("/run/coppermind")

    # Bind addresses and ports.
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    store_host: str = "0.0.0.0"
    store_port: int = 8081

    # Where the API and the workers reach the store.
    store_url: str = "http://store:8081"
    store_timeout_s: float = 30.0

    # PostgreSQL. Supply `database_url` to point at a database you already run;
    # otherwise the parts below are assembled with the password read from
    # `db_password_file`, so the password is never an environment value.
    database_url: str | None = None
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "coppermind"
    db_user: str = "coppermind"
    db_password_file: Path = Path("/run/coppermind/postgres-password")

    internal_token_file: Path = Path("/run/coppermind/internal-token")

    log_level: str = "INFO"

    # Ownership the bootstrap one-shot applies. Every Coppermind image runs
    # as uid 1000 so the store, the Git helper and the sync client can all
    # write the same volume. The bundled PostgreSQL image runs as 999 and
    # needs to read its own password file.
    run_uid: int = 1000
    run_gid: int = 1000
    postgres_uid: int = 999

    @property
    def notes_dir(self) -> Path:
        return self.data_dir / "notes"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def read_internal_token(self) -> str:
        return self.internal_token_file.read_text(encoding="utf-8").strip()

    def _password(self) -> str:
        return self.db_password_file.read_text(encoding="utf-8").strip()

    def database_url_for(self, driver: str) -> str:
        """Return the SQLAlchemy URL for `driver` ("asyncpg" or "psycopg").

        A `database_url` supplied by the deployer wins, with its driver
        replaced so the same value serves both the services and alembic.
        """
        if self.database_url:
            scheme, _, rest = self.database_url.partition("://")
            base = scheme.split("+", 1)[0]
            return f"{base}+{driver}://{rest}"
        from urllib.parse import quote

        password = quote(self._password(), safe="")
        return (
            f"postgresql+{driver}://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )
