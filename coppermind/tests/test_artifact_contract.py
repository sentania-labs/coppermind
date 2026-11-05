"""The artifact half of the store contract: declared kind, one body, the schema's refusal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coppermind.schema import (
    ARTIFACT_KIND,
    OPTIONAL_ROLES,
    REFERENCE_TAG,
    FrontmatterSchema,
    default_schema,
)
from coppermind.store_protocol import (
    ARTIFACT_KIND_MISSING,
    ARTIFACT_KIND_WRONG,
    CreateArtifact,
    SearchQuery,
)

NOTE = {
    "schema_version": 1,
    "id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
    "type": "work",
    "context": "internal",
    "reviewed": False,
    "sources": [],
}


def messages(error: ValidationError) -> list[str]:
    return [str(item["msg"]).removeprefix("Value error, ") for item in error.errors()]


def test_an_upload_without_a_declared_kind_is_refused_with_the_reason():
    with pytest.raises(ValidationError) as raised:
        CreateArtifact(filename="deck.pptx", content_base64="aGk=")
    assert messages(raised.value) == [ARTIFACT_KIND_MISSING]


def test_a_note_kind_is_refused_on_the_artifact_route():
    with pytest.raises(ValidationError) as raised:
        CreateArtifact(kind="note", filename="deck.pptx", content_base64="aGk=")
    assert messages(raised.value) == [ARTIFACT_KIND_WRONG]


def test_exactly_one_of_url_or_bytes_and_a_filename_for_bytes():
    with pytest.raises(ValidationError) as raised:
        CreateArtifact(kind="artifact")
    assert "exactly one of url or content_base64 is required" in messages(raised.value)
    with pytest.raises(ValidationError) as raised:
        CreateArtifact(kind="artifact", url="https://example.test/a", content_base64="aGk=")
    assert "exactly one of url or content_base64 is required" in messages(raised.value)
    with pytest.raises(ValidationError) as raised:
        CreateArtifact(kind="artifact", content_base64="aGk=")
    assert "filename: required when content_base64 is sent" in messages(raised.value)


@pytest.mark.parametrize("filename", ["../x.pdf", "a/b.pdf", "a\\b.pdf", ".", "", "a\x00.pdf"])
def test_a_filename_is_a_name_not_a_path(filename: str):
    with pytest.raises(ValidationError):
        CreateArtifact(kind="artifact", filename=filename, content_base64="aGk=")


@pytest.mark.parametrize("url", ["ftp://host/x", "javascript:alert(1)", "https://", "not a url"])
def test_a_link_must_be_a_web_url(url: str):
    with pytest.raises(ValidationError):
        CreateArtifact(kind="artifact", url=url)


def test_a_link_and_a_file_are_both_valid_artifacts():
    link = CreateArtifact(kind="artifact", url="https://example.test/paper", title="  A   paper ")
    assert link.title == "A paper"
    assert link.filename is None
    upload = CreateArtifact(
        kind="artifact",
        filename="VCDX design.pptx",
        content_base64="aGk=",
        tags=["reference", "vcdx", "reference"],
        mime_type="APPLICATION/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    assert upload.bytes() == b"hi"
    assert upload.tags == ["reference", "vcdx"]
    assert upload.mime_type is not None
    assert upload.mime_type.startswith("application/vnd.openxmlformats")


def test_tags_are_single_words():
    with pytest.raises(ValidationError):
        CreateArtifact(kind="artifact", url="https://example.test/a", tags=["two words"])
    with pytest.raises(ValidationError):
        CreateArtifact(kind="artifact", url="https://example.test/a", tags=["#tag"])


def test_search_takes_a_kind_and_a_citing_note_filter():
    query = SearchQuery.model_validate({"q": "deck", "kind": "artifact", "citing_note": "01K4"})
    assert query.kind == "artifact"
    assert query.citing_note == "01K4"
    with pytest.raises(ValidationError):
        SearchQuery.model_validate({"q": "deck", "kind": "attachment"})


def test_the_shipped_schema_declares_kind_the_citation_key_and_the_reference_tag():
    schema = default_schema()
    keys = {definition.name: definition for definition in schema.keys}
    assert keys["kind"].vocabulary == ["note", "artifact"]
    assert keys["kind"].default == "note"
    assert keys["kind"].required is False
    assert keys["artifacts"].kind == "list"
    assert keys["artifacts"].default is None
    assert schema.roles["kind_key"] == "kind"
    assert schema.roles["artifacts_key"] == "artifacts"
    assert REFERENCE_TAG in schema.tags
    assert schema.defaults()["kind"] == "note"
    assert "artifacts" not in schema.defaults()


def test_a_note_declaring_itself_an_artifact_is_refused_on_every_write_path():
    """`validate_frontmatter` is what create, ingest, replace, patch and adoption all ask."""
    schema = default_schema()
    assert schema.validate_frontmatter(NOTE) == []
    assert schema.validate_frontmatter({**NOTE, "kind": "note"}) == []
    problems = schema.validate_frontmatter({**NOTE, "kind": ARTIFACT_KIND})
    assert len(problems) == 1
    assert problems[0].startswith("kind: an artifact is never a note")
    assert "POST /v1/artifacts" in problems[0]
    assert schema.invalid_keys({**NOTE, "kind": ARTIFACT_KIND}) == ["kind"]


def test_a_saved_schema_that_predates_the_kind_key_still_loads_and_refuses_it():
    """Older schema files name neither optional role; the shipped key stands in."""
    body = default_schema().model_dump(mode="json")
    body["roles"] = {k: v for k, v in body["roles"].items() if k not in OPTIONAL_ROLES}
    body["keys"] = [key for key in body["keys"] if key["name"] not in {"kind", "artifacts"}]
    schema = FrontmatterSchema.model_validate(body)
    assert schema.optional_role("kind_key") == "kind"
    assert schema.optional_role("artifacts_key") == "artifacts"
    assert schema.validate_frontmatter({**NOTE, "kind": "artifact"}) == [
        "kind: an artifact is never a note; store the file or link through POST /v1/artifacts "
        "and cite it from the note instead"
    ]
