"""Source artifact identity framing."""

from coppermind_store.sources import _content_identity


def test_artifact_identity_unambiguously_frames_names_and_hashes():
    first_hash = "a" * 64
    second_hash = "b" * 64
    two_artifacts = [
        {"name": "A", "sha256": first_hash},
        {"name": "B", "sha256": second_hash},
    ]
    one_artifact = [
        {"name": f"A:{first_hash}\nB", "sha256": second_hash},
    ]

    legacy_two = "\n".join(f"{item['name']}:{item['sha256']}" for item in two_artifacts)
    legacy_one = "\n".join(f"{item['name']}:{item['sha256']}" for item in one_artifact)
    assert legacy_two == legacy_one
    assert _content_identity(two_artifacts) != _content_identity(one_artifact)
