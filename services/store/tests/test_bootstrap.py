"""The one-shot that makes a clean checkout runnable with no manual setup."""

from pathlib import Path

from coppermind_store.bootstrap import run

from coppermind.settings import Wiring


def wiring_for(tmp_path: Path) -> Wiring:
    return Wiring(
        data_dir=tmp_path / "data",
        internal_token_file=tmp_path / "internal" / "internal-token",
        db_password_file=tmp_path / "postgres" / "postgres-password",
    )


def test_it_creates_the_trees_secrets_and_settings_a_fresh_install_needs(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    assert run(wiring) == 0

    for directory in (wiring.notes_dir, wiring.sources_dir, wiring.state_dir):
        assert directory.is_dir()
    assert wiring.internal_token_file.read_text(encoding="utf-8").strip()
    assert wiring.db_password_file.read_text(encoding="utf-8").strip()
    assert (wiring.state_dir / "settings.yaml").is_file()
    assert (wiring.state_dir / "schema.yaml").is_file()


def test_the_internal_token_is_not_readable_by_anyone_else(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    run(wiring)
    assert wiring.internal_token_file.stat().st_mode & 0o077 == 0


def test_running_twice_never_rotates_a_secret_or_resets_a_setting(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    run(wiring)
    token = wiring.internal_token_file.read_text(encoding="utf-8")
    password = wiring.db_password_file.read_text(encoding="utf-8")

    settings_file = wiring.state_dir / "settings.yaml"
    settings_file.write_text(
        settings_file.read_text(encoding="utf-8").replace("Review", "Inbox"), encoding="utf-8"
    )

    run(wiring)
    assert wiring.internal_token_file.read_text(encoding="utf-8") == token
    assert wiring.db_password_file.read_text(encoding="utf-8") == password
    assert "Inbox" in settings_file.read_text(encoding="utf-8")
