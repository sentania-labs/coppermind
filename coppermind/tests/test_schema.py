from coppermind.schema import default_schema


def test_the_shipped_schema_covers_every_role():
    schema = default_schema()
    for role in (
        "id_key",
        "date_key",
        "type_key",
        "context_key",
        "account_key",
        "reviewed_key",
        "sources_key",
        "tags_key",
        "schema_version_key",
    ):
        assert schema.key(schema.role(role)) is not None


def test_a_fresh_install_has_working_defaults():
    defaults = default_schema().defaults()
    assert defaults["type"] == "note"
    assert defaults["context"] == "internal"
    assert defaults["reviewed"] is False
    assert defaults["sources"] == []


def test_a_complete_note_validates():
    schema = default_schema()
    assert (
        schema.validate_frontmatter(
            {
                "schema_version": 1,
                "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
                "date": "2026-09-08",
                "type": "meeting",
                "context": "internal",
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


def test_account_is_required_for_a_customer_note():
    schema = default_schema()
    base = {
        "schema_version": 1,
        "id": "x",
        "date": "2026-09-08",
        "type": "meeting",
        "context": "customer",
        "reviewed": False,
        "sources": [],
    }
    assert any(problem.startswith("account:") for problem in schema.validate_frontmatter(base))
    assert schema.validate_frontmatter({**base, "account": "Ameren"}) == []


def test_a_missing_required_key_is_reported():
    problems = default_schema().validate_frontmatter({"type": "note"})
    assert any(problem.startswith("id:") for problem in problems)


def test_a_persons_own_keys_are_not_problems():
    schema = default_schema()
    problems = schema.validate_frontmatter(
        {
            "schema_version": 1,
            "id": "x",
            "date": "2026-09-08",
            "type": "note",
            "context": "internal",
            "reviewed": False,
            "sources": [],
            "aliases": ["something they added themselves"],
        }
    )
    assert problems == []
