from coppermind_store.voice_matching import cosine, matches


def test_matching_model_version_changeover_and_thresholds():
    old = {"model": "old", "version": "1", "vector": [1, 0]}
    new = {"model": "new", "version": "2", "vector": [1, 0]}
    saved = [
        dict(old, person_id="alice"),
        {"model": "old", "version": "1", "vector": [0.5, 0.866025403784], "person_id": "bob"},
        dict(new, version="different", person_id="charlie"),
    ]
    result = matches([old, new], saved)
    assert [(x["person_id"], x["strength"]) for x in result] == [
        ("alice", "strong"),
        ("bob", "maybe"),
    ]
    assert all("vector" not in x for x in result)


def test_invalid_vectors_do_not_match():
    assert cosine([0, 0], [1, 0]) is None
    assert cosine([1], [1, 2]) is None
    assert cosine([float("nan")], [1]) is None
