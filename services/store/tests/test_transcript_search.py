from coppermind_store.transcript_search import passages


def test_passages_are_bounded_deterministic_and_quote_original():
    transcript = "A" * 2000 + "\nArthur Smith: the migration is on Tuesday.\n" + "B" * 2000
    selected, omitted = passages([transcript], {"Arthur Smith"}, 1000)
    assert selected == passages([transcript], {"Arthur Smith"}, 1000)[0]
    assert len(selected) == 1
    assert selected[0] in transcript
    assert "the migration is on Tuesday" in selected[0]
    assert omitted == len(transcript) - len(selected[0])
    assert omitted > 0


def test_no_candidates_sends_no_transcript():
    assert passages(["private transcript"], set()) == ([], 18)
