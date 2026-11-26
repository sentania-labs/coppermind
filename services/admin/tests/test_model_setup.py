from coppermind_admin import model


def _view(calendar):
    return {
        "setup": {},
        "sync": {"vault_name": "Vault", "state": "ok"},
        "companion": {"paired": True},
        "sugg": {"state": "on"},
        "trans": {"state": "on"},
        "calendar": calendar,
    }


def _calendar_step(calendar):
    return next(step for step in model.setup_steps(_view(calendar)) if step.key == "cal")


def test_a_calendar_that_was_not_read_is_not_shown_as_missing():
    unread = {"files": 0, "conflicts": [], "bad_files": [], "unavailable": True}
    assert _calendar_step(unread).done


def test_no_calendar_files_still_leaves_the_calendar_step_open():
    none = {"files": 0, "conflicts": [], "bad_files": []}
    assert not _calendar_step(none).done
