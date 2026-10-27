import pytest
from pydantic import ValidationError

from coppermind.schema import FrontmatterSchema, default_schema


def test_a_schema_missing_a_required_role_is_rejected():
    body = default_schema().model_dump(mode="json")
    del body["roles"]["sources_key"]
    with pytest.raises(ValidationError, match="sources_key"):
        FrontmatterSchema.model_validate(body)


def test_a_schema_role_pointing_at_an_undefined_key_is_rejected():
    body = default_schema().model_dump(mode="json")
    body["roles"]["sources_key"] = "source_ids"
    with pytest.raises(ValidationError, match="sources_key=source_ids"):
        FrontmatterSchema.model_validate(body)


def test_a_fresh_install_has_working_defaults():
    defaults = default_schema().defaults()
    assert defaults["type"] == "reference"
    assert defaults["context"] == "internal"
    assert defaults["reviewed"] is False
    assert defaults["sources"] == []


@pytest.mark.parametrize("note_type", ["work", "personal", "reference", "journal"])
@pytest.mark.parametrize("context", ["internal", "external", "personal"])
def test_a_complete_note_validates(note_type, context):
    schema = default_schema()
    assert (
        schema.validate_frontmatter(
            {
                "schema_version": 1,
                "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
                "date": "2026-09-08",
                "type": note_type,
                "context": context,
                "account": "Acme",
                "reviewed": False,
                "sources": [],
            }
        )
        == []
    )


def test_a_value_outside_the_vocabulary_is_reported():
    schema = default_schema()
    problems = schema.validate_frontmatter(
        {
            "schema_version": 1,
            "id": "x",
            "date": "2026-09-08",
            "type": "podcast",
            "context": "internal",
            "reviewed": False,
            "sources": [],
        }
    )
    assert any("podcast" in problem for problem in problems)


def test_default_schema_requires_account_for_external_notes():
    schema = default_schema()
    base = {
        "schema_version": 1,
        "id": "x",
        "date": "2026-09-08",
        "type": "work",
        "context": "external",
        "reviewed": False,
        "sources": [],
    }
    assert any(problem.startswith("account:") for problem in schema.validate_frontmatter(base))
    assert schema.validate_frontmatter({**base, "account": "Acme"}) == []


def test_a_missing_required_key_is_reported():
    problems = default_schema().validate_frontmatter({"type": "reference"})
    assert any(problem.startswith("id:") for problem in problems)


def test_a_persons_own_keys_are_not_problems():
    schema = default_schema()
    problems = schema.validate_frontmatter(
        {
            "schema_version": 1,
            "id": "x",
            "date": "2026-09-08",
            "type": "reference",
            "context": "internal",
            "reviewed": False,
            "sources": [],
            "aliases": ["something they added themselves"],
        }
    )
    assert problems == []


@pytest.mark.parametrize("context", ["internal", "personal"])
def test_account_is_optional_outside_external_context(context):
    schema = default_schema()
    frontmatter = {
        **schema.defaults(),
        "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
        "date": "2026-09-08",
        "context": context,
    }
    assert "account" not in frontmatter
    assert schema.validate_frontmatter(frontmatter) == []


def test_parse_note_time_and_date_derivation_and_mismatch():
    from datetime import date, time

    from coppermind import frontmatter as fm
    from coppermind.schema import note_date, note_date_mismatch, parse_note_time

    schema = default_schema()
    parsed, _ = fm.parse("---\ntime: 2026-10-20T12:00:00\n---\n# Note\n")
    info = parse_note_time(parsed["time"])
    assert info.valid is True
    assert info.has_offset is False
    assert info.date_value == date(2026, 10, 20)
    assert info.time_value == time(12, 0, 0)
    assert note_date(parsed, schema) == date(2026, 10, 20)

    legacy = parse_note_time("09:30")
    assert legacy.valid is True
    assert legacy.date_value is None
    assert legacy.time_value == time(9, 30, 0)

    with_offset = parse_note_time("2026-10-20T12:00:00-05:00")
    assert with_offset.valid is False
    assert with_offset.has_offset is True

    disagree = {"date": "2026-10-19", "time": "2026-10-20T12:00:00"}
    assert note_date(disagree, schema) == date(2026, 10, 19)
    assert note_date_mismatch(disagree, schema) == (date(2026, 10, 19), date(2026, 10, 20))
    assert note_date({"time": "2026-10-20T12:00:00"}, schema) == date(2026, 10, 20)


def test_default_schema_declares_account_as_a_list_and_no_time_key():
    schema = default_schema()
    keys = {key.name: key for key in schema.keys}
    assert keys["account"].kind == "list"
    assert "list" in keys["account"].description
    assert keys["date"].kind == "date"
    assert keys["date"].required is False
    assert "YYYY-MM-DDTHH:MM:SS" in keys["date"].description
    assert "time" not in keys and "timezone" not in keys


def external(account):
    return {
        "schema_version": 1,
        "id": "01ID",
        "type": "work",
        "context": "external",
        "reviewed": False,
        "sources": [],
        "date": "2026-10-20",
        "account": account,
    }


def test_account_string_and_one_item_list_validate_the_same():
    schema = default_schema()
    assert schema.validate_frontmatter(external("Xcel")) == []
    assert schema.validate_frontmatter(external(["Xcel"])) == []
    assert schema.validate_frontmatter(external(["Xcel", "United"])) == []


def test_external_note_with_an_empty_account_fails_like_a_missing_one():
    schema = default_schema()
    missing = external("Xcel")
    del missing["account"]
    expected = schema.validate_frontmatter(missing)
    assert expected == ["account: required when context is one of external"]
    for empty in ([], "", "  ", [""], None):
        assert schema.validate_frontmatter(external(empty)) == expected
    assert schema.invalid_keys(external([])) == ["account"]


def test_an_internal_reference_note_may_list_several_accounts_or_none():
    schema = default_schema()
    for account in (["Xcel", "United"], [], "Xcel"):
        note = external(account) | {"context": "internal", "type": "reference"}
        assert schema.validate_frontmatter(note) == []


def test_account_entries_must_be_text():
    problems = default_schema().validate_frontmatter(external([1, "Xcel"]))
    assert problems == ["account: expected text entries"]
    assert default_schema().validate_frontmatter(external(5)) == ["account: expected a list"]


def test_account_list_reads_strings_lists_and_nothing():
    from coppermind.schema import account_list

    assert account_list("Xcel") == ["Xcel"]
    assert account_list(["Xcel", "United", "Xcel", " "]) == ["Xcel", "United"]
    assert account_list(None) == [] and account_list([]) == [] and account_list(3) == []


def test_date_accepts_a_day_or_a_local_datetime_and_rejects_an_offset():
    from datetime import date, datetime

    schema = default_schema()
    base = external("Xcel")
    for value in (
        "2026-10-20",
        "2026-10-20T12:00:00",
        date(2026, 10, 20),
        datetime(2026, 10, 20, 12, 0, 0),
    ):
        assert schema.validate_frontmatter(base | {"date": value}) == []
    assert schema.validate_frontmatter(base | {"date": "2026-10-20T12:00:00-05:00"}) == [
        "date: must be a local date or date-and-time without a UTC offset"
    ]
    assert schema.validate_frontmatter(base | {"date": "2026-10-20T12:00:00Z"}) == [
        "date: must be a local date or date-and-time without a UTC offset"
    ]
    assert schema.validate_frontmatter(base | {"date": "tomorrow"}) == [
        "date: expected a date as YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
    ]


def test_date_and_datetime_give_the_same_day_and_start_time():
    from datetime import date, time

    from coppermind import frontmatter as fm
    from coppermind.schema import note_date, note_start_time

    schema = default_schema()
    parsed, _ = fm.parse("---\ndate: 2026-10-20T12:00:00\n---\n# Note\n")
    assert note_date(parsed, schema) == date(2026, 10, 20)
    assert note_start_time(parsed, schema) == time(12, 0)
    assert note_start_time({"date": "2026-10-20"}, schema) is None
    assert note_start_time({"date": "2026-10-20", "time": "09:30"}, schema) == time(9, 30)
    assert note_date({"date": "2026-10-20 12:00:00"}, schema) == date(2026, 10, 20)


def test_a_date_suggestion_writes_date_only_and_removes_the_legacy_time_and_timezone():
    from coppermind.schema import field_suggestion_changes

    schema = default_schema()
    legacy = {"time": "2026-10-14T10:00:00", "timezone": "America/New_York"}
    assert field_suggestion_changes(legacy, schema, "date", "2026-10-15T09:00:00") == (
        {"date": "2026-10-15T09:00:00"},
        ["time", "timezone"],
    )
    assert field_suggestion_changes({"date": "2026-10-14"}, schema, "time", "09:30") == (
        {"date": "2026-10-14T09:30:00"},
        ["time", "timezone"],
    )


def test_an_account_suggestion_joins_the_list_and_never_replaces_it():
    from coppermind.schema import field_suggestion_changes

    schema = default_schema()
    held = {"account": ["Xcel", "United"]}
    assert field_suggestion_changes(held, schema, "account", "Example") == (
        {"account": ["Xcel", "United", "Example"]},
        [],
    )
    assert field_suggestion_changes(held, schema, "account", ["United", "Acme"]) == (
        {"account": ["Xcel", "United", "Acme"]},
        [],
    )
    assert field_suggestion_changes({"account": "Xcel"}, schema, "account", "Acme")[0] == {
        "account": ["Xcel", "Acme"]
    }
    assert field_suggestion_changes({}, schema, "account", "Example")[0] == {"account": ["Example"]}
    assert field_suggestion_changes({"account": []}, schema, "account", ["A", "B"])[0] == {
        "account": ["A", "B"]
    }
