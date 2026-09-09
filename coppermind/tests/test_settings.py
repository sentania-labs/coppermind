from pathlib import Path

from coppermind.settings import GIB, MIB, ProductSettings, Wiring, default_settings


def test_a_fresh_install_runs_on_shipped_defaults():
    settings = default_settings()
    assert settings.notes.review_folder == "Review"
    assert settings.notes.trash_folder == "_Trash"
    assert settings.general.timezone == "America/Chicago"
    assert settings.git.enabled is True
    assert settings.reconcile.scan_interval_s == 60


def test_sync_limits_follow_the_plan_unless_they_are_set():
    standard = ProductSettings()
    assert standard.sync.effective_limits() == (5 * MIB, 1 * GIB)

    plus = ProductSettings.model_validate({"sync": {"plan": "plus"}})
    assert plus.sync.effective_limits() == (200 * MIB, 10 * GIB)

    override = ProductSettings.model_validate({"sync": {"max_file_bytes": 1234}})
    assert override.sync.effective_limits()[0] == 1234


def test_the_attachment_limit_never_exceeds_what_sync_can_carry():
    assert ProductSettings().attachment_limit_bytes() == 5 * MIB


def test_settings_survive_a_round_trip_through_a_file_body():
    body = default_settings().model_dump(mode="json")
    assert ProductSettings.model_validate(body) == default_settings()


def test_wiring_assembles_a_url_from_a_password_file(tmp_path: Path):
    password = tmp_path / "postgres-password"
    # A character that has to be percent encoded, because a generated password
    # containing one must not change how the URL parses.
    password.write_text("pa/ss word\n", encoding="utf-8")
    wiring = Wiring(db_password_file=password, db_host="db", db_name="cm", db_user="cm")
    assert wiring.database_url_for("asyncpg") == (
        "postgresql+asyncpg://cm:pa%2Fss%20word@db:5432/cm"
    )


def test_a_supplied_url_wins_and_takes_the_driver_it_is_asked_for():
    wiring = Wiring(database_url="postgresql://cm@db.example:5432/cm")
    assert wiring.database_url_for("psycopg") == "postgresql+psycopg://cm@db.example:5432/cm"
    assert wiring.database_url_for("asyncpg") == "postgresql+asyncpg://cm@db.example:5432/cm"


def test_wiring_defaults_point_at_the_compose_stack():
    wiring = Wiring()
    assert wiring.store_url == "http://store:8081"
    assert wiring.notes_dir == Path("/data/notes")
    assert wiring.state_dir == Path("/data/state")
