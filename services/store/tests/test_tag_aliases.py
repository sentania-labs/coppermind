from coppermind.schema import default_schema


def test_normalize_tags():
    schema = default_schema()
    schema.tag_aliases = {"t1": "tag1", "t2": "tag2", "t3": "tag1"}

    assert schema.normalize_tags(["t1", "t2", "unknown"]) == ["tag1", "tag2", "unknown"]
    assert schema.normalize_tags(["t1", "tag1", "t3"]) == ["tag1"]
