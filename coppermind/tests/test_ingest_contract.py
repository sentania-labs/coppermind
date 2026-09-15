import base64

import pytest
from pydantic import ValidationError

from coppermind.store_protocol import IngestArtifact, IngestRequest


def request_with(artifacts):
    return {
        "source": {
            "provider": "plaud",
            "external_source_id": "recording-1",
            "source_type": "transcript",
            "artifacts": artifacts,
        },
        "note": {"title": "Recording"},
    }


@pytest.mark.parametrize("name", ["../secret", "nested/file", "nested\\file", "..", "bad\0name"])
def test_artifact_names_cannot_leave_the_revision_directory(name):
    with pytest.raises(ValidationError, match="filename, not a path"):
        IngestRequest.model_validate(
            request_with([{"name": name, "mime_type": "text/plain", "content": "x"}])
        )


def test_artifacts_have_one_content_form_and_unique_names():
    with pytest.raises(ValidationError, match="exactly one"):
        IngestRequest.model_validate(
            request_with(
                [
                    {
                        "name": "transcript.txt",
                        "mime_type": "text/plain",
                        "content": "x",
                        "content_base64": "eA==",
                    }
                ]
            )
        )
    with pytest.raises(ValidationError, match="names must be unique"):
        IngestRequest.model_validate(
            request_with(
                [
                    {"name": "same.txt", "mime_type": "text/plain", "content": "one"},
                    {"name": "same.txt", "mime_type": "text/plain", "content": "two"},
                ]
            )
        )


def test_base64_artifacts_decode_to_their_original_bytes():
    original = b"\x00binary\xff"
    artifact = IngestArtifact(
        name="recording.bin",
        mime_type="application/octet-stream",
        content_base64=base64.b64encode(original).decode("ascii"),
    )
    assert artifact.bytes() == original
