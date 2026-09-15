"""The interim key-rotation command writes hashes and reveals once."""

from pathlib import Path

from coppermind_store.control import ControlState
from coppermind_store.keys import run

from coppermind.api_keys import split_credential, verify_secret
from coppermind.settings import Wiring


def test_create_command_prints_a_key_and_persists_only_its_hash(tmp_path: Path, capsys) -> None:
    wiring = Wiring(data_dir=tmp_path / "data")
    wiring.state_dir.mkdir(parents=True)

    assert run(["create", "--name", "read only", "--scope", "notes:read"], wiring) == 0

    credential = capsys.readouterr().out.strip()
    parsed = split_credential(credential)
    assert parsed is not None
    key_id, secret = parsed
    key_set = ControlState(wiring.state_dir).api_keys()
    assert len(key_set.keys) == 1
    assert key_set.keys[0].key_id == key_id
    assert key_set.keys[0].scopes == ["notes:read"]
    assert verify_secret(key_set.keys[0].hash, secret)
    control_text = (wiring.state_dir / "keys.json").read_text(encoding="utf-8")
    assert credential not in control_text
    assert secret not in control_text

    assert run(["revoke", key_id], wiring) == 0
    assert ControlState(wiring.state_dir).api_keys().keys[0].revoked_at is not None


def test_an_unknown_key_id_is_an_error_message_not_a_traceback(tmp_path: Path, capsys) -> None:
    wiring = Wiring(data_dir=tmp_path / "data")
    wiring.state_dir.mkdir(parents=True)

    assert run(["revoke", "0123456789abcdef"], wiring) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "0123456789abcdef" in captured.err
