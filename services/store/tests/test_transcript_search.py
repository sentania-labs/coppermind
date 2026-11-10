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


def test_whole_words_only_and_specific_terms_spend_budget_first():
    noise = ("Target needed targets. " + "x" * 700 + "\n") * 5
    real = "Arthur Smith: Ed will own the Target rollout."
    transcript = noise + "z" * 2000 + "\n" + real
    selected, _ = passages([transcript], {"Ed", "Target", "Arthur Smith"}, 800)
    joined = "".join(selected)
    assert "Arthur Smith: Ed will own" in joined
    assert sum(map(len, selected)) <= 800


def test_terms_inside_longer_words_do_not_match():
    text = "Edward needed targets"
    assert passages([text], {"Ed", "Target"}) == ([], len(text))
