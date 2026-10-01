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

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from coppermind.naming import sanitize_folder
from coppermind.statefiles import StateStore

MIB = 1024 * 1024

SyncPlan = Literal["standard", "plus"]


class GeneralSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(
        default="America/Chicago", description="Timezone for local dates and scheduled work."
    )

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

    review_folder: str = Field(default="Review", description="Folder for notes awaiting review.")
    trash_folder: str = Field(default="_Trash", description="Folder for deleted notes.")
    sources_folder: str = Field(
        default="_Sources",
        description=(
            "Folder for generated source projections. Renaming it after sources exist "
            "strands existing projections; move them deliberately before changing it."
        ),
    )
    attachments_folder: str = Field(
        default="_Attachments", description="Folder for note attachments."
    )
    journal_folder: str = Field(default="Journal", description="Folder for journal notes.")
    dated_types: list[str] = Field(
        default_factory=lambda: ["meeting", "journal"],
        description="Note types whose filenames start with a date.",
    )

    @field_validator("sources_folder")
    @classmethod
    def validate_sources_folder(cls, value: str) -> str:
        """Refuse a name the Store and the Git helper would resolve differently.

        The Store writes projections to the name as configured, and the Git
        helper, which carries none of this package, excludes that same name as
        it is given. A value either of them would spell differently would put
        every generated transcript into Git history, so it is refused here
        rather than accepted and split.
        """
        portable = sanitize_folder(value)
        if not portable:
            raise ValueError(
                f"{value!r} names no folder, so the Store would write projections into the notes "
                "filesystem root while the Git helper kept excluding the folder it last "
                "accepted; give a folder name the two can both hold"
            )
        if portable != value:
            raise ValueError(
                f"{value!r} is written to the notes filesystem as {portable!r}, and the Git "
                f"exclusion uses the name as given; the two must match, so configure "
                f"{portable!r}"
            )
        return value


class ReconcileSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scan_interval_s: int = Field(default=60, ge=1, description="Seconds between filesystem scans.")
    quiet_period_s: int = Field(
        default=30, ge=0, description="Seconds a file must remain quiet before adoption."
    )
    # Local wall clock, in the timezone under `general`. The reconciler reads
    # this every interval, so it is checked here rather than where it is used.
    full_rehash_daily_at: str = Field(
        default="03:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Local time for the daily full rehash, in HH:MM format.",
    )


class GitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Enable Git history.")
    debounce_s: int = Field(
        default=60, ge=1, description="Seconds to wait after an edit before committing."
    )
    poll_interval_s: int = Field(
        default=300, ge=1, description="Seconds between remote polling attempts."
    )
    identity_name: str = Field(default="Coppermind", description="Name used for Git commits.")
    identity_email: str = Field(
        default="coppermind@localhost",
        description="Email identity used for Git commits, not an operator login.",
    )
    gc_auto: bool = Field(default=True, description="Allow automatic Git garbage collection.")

    @field_validator("identity_name", "identity_email")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"git.{info.field_name} must not be blank")
        return value


class SyncSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: SyncPlan = Field(
        default="standard", description="Obsidian Sync plan used to derive size limits."
    )
    # Null uses the selected plan's limits. Explicit overrides remain available.
    max_file_bytes: int | None = Field(
        default=None, description="File size limit in bytes; null uses the plan limit."
    )
    max_total_bytes: int | None = Field(
        default=None, description="Total size limit in bytes; null uses the plan limit."
    )
    device_name: str = Field(
        default="coppermind-server", description="Device name shown by Obsidian Sync."
    )
    mode: str = Field(default="bidirectional", description="Sync direction mode.")
    conflict_strategy: str = Field(
        default="merge", description="How sync resolves conflicting edits."
    )
    excluded_folders: list[str] = Field(
        default_factory=lambda: ["_Trash"],
        description="Folders excluded from syncing, as a JSON array.",
    )
    file_types: list[str] = Field(
        default_factory=lambda: ["image", "audio", "video", "pdf"],
        description="Attachment types to sync, as a JSON array.",
    )
    sync_configs: list[str] = Field(
        default_factory=list, description="Configuration categories to sync, as a JSON array."
    )

    @field_validator("device_name")
    @classmethod
    def validate_device_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sync.device_name must not be blank")
        return value

    @property
    def file_limit_bytes(self) -> int:
        """Resolve the per-file ceiling from the plan unless explicitly overridden."""
        if self.max_file_bytes is not None:
            return self.max_file_bytes
        return (5 if self.plan == "standard" else 200) * MIB

    @property
    def total_limit_bytes(self) -> int:
        """Resolve the base plan storage; Plus add-ons use the existing override."""
        if self.max_total_bytes is not None:
            return self.max_total_bytes
        return (1 if self.plan == "standard" else 10) * 1024 * MIB


class CuratorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Enable automatic filing.")
    sweep_interval_s: int = Field(default=300, description="Seconds between filing sweeps.")
    inbox_only: bool = Field(default=True, description="Restrict filing to the inbox.")


class IndexerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Enable search indexing.")
    language: str = Field(default="english", description="Language used for text search.")
    reconcile_interval_s: int = Field(
        default=600, description="Seconds between index reconciliation passes."
    )


class EventSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_days: int = Field(default=7, description="Days to retain events.")
    poll_fallback_s: int = Field(default=5, description="Seconds between fallback event polls.")


class LimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingest_max_bytes: int = Field(
        default=25 * MIB, gt=0, description="Maximum ingest payload in bytes."
    )
    # Null marks this override as unset. Attachment ingest derives the sync
    # file ceiling when its consumer lands.
    attachment_max_bytes: int | None = Field(
        default=None, description="Attachment ceiling in bytes; null uses the sync file limit."
    )


class AdminSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_hours: int = Field(
        default=12, ge=1, le=24 * 365, description="Hours before a signed Admin session expires."
    )


class ProductSettings(BaseModel):
    """The whole of `settings.yaml`, with the defaults a fresh install runs on."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, description="Settings format version.")
    general: GeneralSettings = Field(
        default_factory=GeneralSettings, description="General product settings."
    )
    notes: NotesSettings = Field(
        default_factory=NotesSettings, description="Notes filesystem layout."
    )
    reconcile: ReconcileSettings = Field(
        default_factory=ReconcileSettings, description="Reconciliation schedule."
    )
    git: GitSettings = Field(default_factory=GitSettings, description="Git history settings.")
    sync: SyncSettings = Field(default_factory=SyncSettings, description="Obsidian Sync settings.")
    curator: CuratorSettings = Field(
        default_factory=CuratorSettings, description="Automatic filing settings."
    )
    indexer: IndexerSettings = Field(
        default_factory=IndexerSettings, description="Search indexing settings."
    )
    events: EventSettings = Field(
        default_factory=EventSettings, description="Event processing settings."
    )
    limits: LimitSettings = Field(default_factory=LimitSettings, description="Payload limits.")
    admin: AdminSettings = Field(
        default_factory=AdminSettings, description="Admin session settings."
    )


def read_settings(store: StateStore) -> ProductSettings:
    """The one way `settings.yaml` becomes ProductSettings, for every reader."""
    body = dict(store.read("settings").body)
    body.pop("revision", None)
    return ProductSettings.model_validate(body)


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
    default_api_key_file: Path = Path("/run/coppermind/api/default-api-key")

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
