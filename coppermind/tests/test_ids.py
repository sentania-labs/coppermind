from coppermind.ids import is_valid_id, new_id


def test_new_id_is_a_valid_ulid():
    value = new_id()
    assert len(value) == 26
    assert is_valid_id(value)


def test_ids_are_unique_and_sort_by_creation():
    first, second = new_id(), new_id()
    assert first != second
    assert first <= second


def test_rejects_things_that_are_not_identifiers():
    assert not is_valid_id("")
    assert not is_valid_id("not-a-ulid")
    assert not is_valid_id(None)
    # I, L, O and U are outside the alphabet, so a transcription slip is caught.
    assert not is_valid_id("01K4Q8Z3N7V2X9M1B5C6D8E0FI")
