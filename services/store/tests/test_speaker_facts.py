"""What a card says about a voice is read from the transcript, never guessed."""

from coppermind_store import speaker_facts as facts


def segment(start, end, speaker, text):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


def test_talk_time_adds_up_each_voices_turns():
    transcript = {
        "segments": [
            segment(0, 30, "A", "x"),
            segment(30, 40, "B", "y"),
            segment(40, 55.5, "A", "z"),
            {"start": None, "end": None, "speaker": "C", "text": "no times"},
        ]
    }
    assert facts.talk_seconds(transcript) == {"A": 45.5, "B": 10.0, "C": 0.0}


def test_quotes_are_word_for_word_detailed_and_spread_over_the_recording():
    text = "We run vSphere 7 on the older cluster today. Yes. Right, okay."
    transcript = {
        "segments": [
            segment(0, 10, "A", text),
            segment(12, 15, "A", "Yeah that is right."),
            segment(300, 320, "A", "The Veeam backup repository sits behind a Fortinet firewall."),
            segment(310, 330, "A", "Our upgrade window is the second weekend of March for sure."),
            segment(900, 920, "A", "I will email the compatibility guide to everyone on Friday."),
        ]
    }
    found = facts.quotes(transcript, "A")
    assert len(found) == 3
    originals = " ".join(s["text"] for s in transcript["segments"])
    assert all(quote in originals for quote, _ in found)
    assert [when for _, when in found] == sorted(when for _, when in found)
    # Two lines ten seconds apart are one moment: the third is taken from elsewhere.
    assert not (
        any("Veeam" in quote for quote, _ in found) and any("March" in quote for quote, _ in found)
    )


def test_a_line_that_would_show_as_markup_is_never_quoted():
    transcript = {
        "segments": [
            segment(
                0, 5, "A", 'He said "never" in a [[odd]] way about <b>this</b> long thing here'
            ),
            segment(10, 15, "A", "Look at https://example.com/page for the whole long story today"),
            segment(20, 25, "A", "That was the whole point \u2014 the rest does not matter at all"),
        ]
    }
    assert facts.quotes(transcript, "A") == []


def test_a_voice_that_only_said_a_few_words_has_no_quotes():
    assert facts.quotes({"segments": [segment(0, 1, "A", "Hi.")]}, "A") == []
