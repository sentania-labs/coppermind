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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

MIB = 1024 * 1024

SyncPlan = Literal["standard", "plus"]


class GeneralSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = "America/Chicago"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class NotesSettings(BaseModel):
    """Folder layout of the notes filesystem. Every name is renameable."""

    model_config = ConfigDict(extra="forbid")

    review_folder: str = "Review"
    trash_folder: str = "_Trash"
    sources_folder: str = "_Sources"
    attachments_folder: str = "_Attachments"
    journal_folder: str = "Journal"
    dated_types: list[str] = Field(default_factory=lambda: ["meeting", "journal"])


class ReconcileSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scan_interval_s: int = 60
    quiet_period_s: int = 30
    full_rehash_daily_at: str = "03:30"


class GitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    debounce_s: int = 60
    poll_interval_s: int = 300
    identity_name: str = "Coppermind"
    identity_email: str = "coppermind@localhost"
    gc_auto: bool = True


class SyncSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: SyncPlan = "standard"
    # Null marks these overrides as unset. Ingest derives plan limits when its
    # consumer lands.
    max_file_bytes: int | None = None
    max_total_bytes: int | None = None
    device_name: str = "coppermind-server"
    mode: str = "bidirectional"
    conflict_strategy: str = "merge"
    excluded_folders: list[str] = Field(default_factory=lambda: ["_Trash"])
    file_types: list[str] = Field(default_factory=lambda: ["image", "audio", "video", "pdf"])
    sync_configs: list[str] = Field(default_factory=list)


class CuratorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    sweep_interval_s: int = 300
    inbox_only: bool = True


class IndexerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    language: str = "english"
    reconcile_interval_s: int = 600


class EventSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_days: int = 7
    poll_fallback_s: int = 5


class LimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingest_max_bytes: int = 25 * MIB
    # Null marks this override as unset. Attachment ingest derives the sync
    # file ceiling when its consumer lands.
    attachment_max_bytes: int | None = None


class AdminSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_hours: int = 12


class ProductSettings(BaseModel):
    """The whole of `settings.yaml`, with the defaults a fresh install runs on."""

    model_config = ConfigDict(extra="forbid")

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


def default_settings() -> ProductSettings:
    """The settings a fresh install runs with."""
    return ProductSettings()


class Wiring(BaseSettings):
    """Deployment wiring. Every value has a default that works on compose."""

    model_config = SettingsConfigDict(env_prefix="COPPERMIND_", extra="ignore")

    data_dir: Path = Path("/data")
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
    db_password_file: Path = Path("/run/coppermind/postgres/postgres-password")

    internal_token_file: Path = Path("/run/coppermind/internal/internal-token")

    log_level: str = "INFO"

    # The image build stamps the tag it built from here. A checkout run
    # straight from the working tree leaves it unset and reports the package
    # version instead, so neither form claims to be something it is not.
    build_version: str | None = None

    # Ownership the bootstrap one-shot applies. Every Coppermind image runs
    # as uid 1000 so the store, the Git helper and the sync client can all
    # write the same volume.
    run_uid: int = 1000
    run_gid: int = 1000

    def running_version(self, package_version: str) -> str:
        """The version a service reports on `/healthz` and in its OpenAPI document."""
        return self.build_version or package_version

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

        A supplied `database_url` carries no password. Its driver is replaced
        and the credential file supplies the password for every database.
        """
        if self.database_url:
            url = make_url(self.database_url)
            if url.password is not None:
                raise ValueError(
                    "COPPERMIND_DATABASE_URL must not contain a password; "
                    "use COPPERMIND_DB_PASSWORD_FILE"
                )
            if url.username is None:
                raise ValueError("COPPERMIND_DATABASE_URL must include a database user")
            url = url.set(
                drivername=f"{url.get_backend_name()}+{driver}", password=self._password()
            )
        else:
            url = URL.create(
                drivername=f"postgresql+{driver}",
                username=self.db_user,
                password=self._password(),
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
            )
        return url.render_as_string(hide_password=False)
