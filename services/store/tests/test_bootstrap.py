"""The one-shot that makes a clean checkout runnable with no manual setup."""

from pathlib import Path
from stat import S_IMODE

from coppermind_store.bootstrap import run
from coppermind_store.control import ControlState

from coppermind.api_keys import split_credential, verify_secret
from coppermind.settings import Wiring


def wiring_for(tmp_path: Path) -> Wiring:
    return Wiring(
        data_dir=tmp_path / "data",
        internal_token_file=tmp_path / "internal" / "internal-token",
        db_password_file=tmp_path / "postgres" / "postgres-password",
        default_api_key_file=tmp_path / "api" / "default-api-key",
    )


def test_it_creates_the_trees_secrets_and_settings_a_fresh_install_needs(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    assert run(wiring) == 0

    for directory in (wiring.notes_dir, wiring.sources_dir, wiring.state_dir):
        assert directory.is_dir()
    assert wiring.internal_token_file.read_text(encoding="utf-8").strip()
    assert wiring.db_password_file.read_text(encoding="utf-8").strip()
    assert wiring.default_api_key_file.read_text(encoding="utf-8").strip()
    assert (wiring.state_dir / "settings.yaml").is_file()
    assert (wiring.state_dir / "schema.yaml").is_file()
    assert (wiring.state_dir / "keys.json").is_file()


def test_the_internal_token_is_not_readable_by_anyone_else(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    run(wiring)
    assert wiring.internal_token_file.stat().st_mode & 0o077 == 0


def test_the_database_password_uses_the_projected_secret_mode(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    run(wiring)
    assert S_IMODE(wiring.db_password_file.stat().st_mode) == 0o644


def test_the_default_api_key_is_revealed_once_but_only_its_hash_is_control_state(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    run(wiring)
    credential = wiring.default_api_key_file.read_text(encoding="utf-8").strip()
    parsed = split_credential(credential)
    assert parsed is not None
    key_id, secret = parsed
    record = ControlState(wiring.state_dir).api_keys().keys[0]
    assert record.key_id == key_id
    assert verify_secret(record.hash, secret)
    assert credential not in (wiring.state_dir / "keys.json").read_text(encoding="utf-8")
    assert secret not in (wiring.state_dir / "keys.json").read_text(encoding="utf-8")
    assert S_IMODE(wiring.default_api_key_file.stat().st_mode) == 0o600
    assert S_IMODE((wiring.state_dir / "keys.json").stat().st_mode) == 0o600


def test_running_twice_never_rotates_a_secret_or_resets_a_setting(tmp_path: Path):
    wiring = wiring_for(tmp_path)
    run(wiring)
    token = wiring.internal_token_file.read_text(encoding="utf-8")
    password = wiring.db_password_file.read_text(encoding="utf-8")
    api_key = wiring.default_api_key_file.read_text(encoding="utf-8")
    key_hash = ControlState(wiring.state_dir).api_keys().keys[0].hash

    settings_file = wiring.state_dir / "settings.yaml"
    settings_file.write_text(
        settings_file.read_text(encoding="utf-8").replace("Review", "Inbox"), encoding="utf-8"
    )

    run(wiring)
    assert wiring.internal_token_file.read_text(encoding="utf-8") == token
    assert wiring.db_password_file.read_text(encoding="utf-8") == password
    assert wiring.default_api_key_file.read_text(encoding="utf-8") == api_key
    assert ControlState(wiring.state_dir).api_keys().keys[0].hash == key_hash
    assert "Inbox" in settings_file.read_text(encoding="utf-8")
