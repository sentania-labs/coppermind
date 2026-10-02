import pytest
from coppermind.schema import default_schema, TagSettings

def test_alias_normalization_in_place():
    schema = default_schema()
    schema.tags = TagSettings(
        open=True,
        meanings={"canonical": "A canonical tag"},
        aliases={"alias1": "canonical", "alias2": "canonical"}
    )
    
    # Normalizes existing tags, keeps unknowns if open
    fm = {"tags": ["alias1", "alias2", "unknown", "canonical"]}
    schema.normalize_tags_in_place(fm)
    assert fm["tags"] == ["canonical", "unknown"]

def test_closed_tags_refuses_unknowns():
    schema = default_schema()
    schema.tags = TagSettings(
        open=False,
        meanings={"canonical": "A canonical tag"},
        aliases={"alias1": "canonical"}
    )
    
    fm = {"tags": ["alias1", "unknown"]}
    schema.normalize_tags_in_place(fm)
    assert fm["tags"] == ["canonical", "unknown"]
    
    problems = schema.validate_frontmatter(fm)
    assert any("refuses unknown tag 'unknown'" in p for p in problems)
