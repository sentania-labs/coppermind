"""The one-shot that makes a clean checkout runnable with no manual setup."""

from pathlib import Path
from stat import S_IMODE

from coppermind_store.bootstrap import run
from coppermind_store.control import ControlState
from coppermind_store.keys import DEFAULT_KEY_NAME, REVOKED_NOTICE, revoke_key

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


def test_an_unusable_reveal_file_mints_a_working_key_instead_of_failing(tmp_path: Path):
    """A damaged reveal file no service reads must not stop the stack starting."""
    wiring = wiring_for(tmp_path)
    wiring.default_api_key_file.parent.mkdir(parents=True)
    wiring.default_api_key_file.write_text("not-a-credential\n", encoding="utf-8")

    assert run(wiring) == 0

    credential = wiring.default_api_key_file.read_text(encoding="utf-8").strip()
    parsed = split_credential(credential)
    assert parsed is not None
    key_id, secret = parsed
    record = ControlState(wiring.state_dir).api_keys().keys[0]
    assert record.key_id == key_id
    assert verify_secret(record.hash, secret)


def test_a_reveal_file_without_its_record_is_replaced_by_a_usable_pair(tmp_path: Path):
    """The crash between the two writes leaves a credential nobody could have used."""
    wiring = wiring_for(tmp_path)
    run(wiring)
    orphan = wiring.default_api_key_file.read_text(encoding="utf-8")
    (wiring.state_dir / "keys.json").unlink()

    assert run(wiring) == 0

    credential = wiring.default_api_key_file.read_text(encoding="utf-8")
    assert credential != orphan
    parsed = split_credential(credential.strip())
    assert parsed is not None
    key_id, secret = parsed
    record = ControlState(wiring.state_dir).api_keys().keys[0]
    assert record.key_id == key_id
    assert verify_secret(record.hash, secret)


def test_a_revoked_default_key_stays_revoked_and_says_so(tmp_path: Path):
    """The documented way to read the default must not hand out a dead credential."""
    wiring = wiring_for(tmp_path)
    run(wiring)
    control = ControlState(wiring.state_dir)
    revoked_id = control.api_keys().keys[0].key_id
    revoke_key(control, revoked_id)

    assert run(wiring) == 0
    assert wiring.default_api_key_file.read_text(encoding="utf-8").strip() == REVOKED_NOTICE
    records = ControlState(wiring.state_dir).api_keys().keys
    assert [record.key_id for record in records] == [revoked_id]
    assert records[0].revoked_at is not None

    assert run(wiring) == 0
    assert wiring.default_api_key_file.read_text(encoding="utf-8").strip() == REVOKED_NOTICE
    assert len(ControlState(wiring.state_dir).api_keys().keys) == 1


def test_a_restored_backup_never_resurrects_a_revoked_default(tmp_path: Path, capsys):
    """The `data` volume carries the revocation; losing the credential volume must not undo it."""
    wiring = wiring_for(tmp_path)
    run(wiring)
    control = ControlState(wiring.state_dir)
    revoked_id = control.api_keys().keys[0].key_id
    revoke_key(control, revoked_id)
    wiring.default_api_key_file.unlink()
    capsys.readouterr()

    assert run(wiring) == 0
    assert "default_api_key=revoked" in capsys.readouterr().out

    records = ControlState(wiring.state_dir).api_keys().keys
    assert [record.key_id for record in records] == [revoked_id]
    assert records[0].revoked_at is not None
    assert wiring.default_api_key_file.read_text(encoding="utf-8").strip() == REVOKED_NOTICE


def test_a_live_default_whose_credential_is_gone_is_re_issued(tmp_path: Path, capsys):
    """A restore that keeps a live default must come up with a usable credential."""
    wiring = wiring_for(tmp_path)
    run(wiring)
    first_id = ControlState(wiring.state_dir).api_keys().keys[0].key_id
    wiring.default_api_key_file.unlink()
    capsys.readouterr()

    assert run(wiring) == 0
    assert "default_api_key=re-issued" in capsys.readouterr().out

    credential = wiring.default_api_key_file.read_text(encoding="utf-8").strip()
    parsed = split_credential(credential)
    assert parsed is not None
    key_id, secret = parsed
    assert key_id != first_id
    records = ControlState(wiring.state_dir).api_keys().keys
    current = next(record for record in records if record.key_id == key_id)
    assert current.name == DEFAULT_KEY_NAME
    assert current.revoked_at is None
    assert verify_secret(current.hash, secret)
