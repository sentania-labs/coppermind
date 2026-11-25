"""What the Speakers section says about a voice is read from the transcript, never guessed."""

from coppermind_store import speaker_facts as facts
from coppermind_store.voice_naming import candidates, could_be


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


def test_a_line_that_would_break_the_note_is_never_quoted():
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


def test_a_name_used_to_answer_a_voice_is_offered_as_its_name_with_the_time():
    transcript = {
        "segments": [
            segment(250, 255, "Scott Bowe", "Thanks for joining, Tom."),
            segment(256, 260, "Unknown voice 2", "Happy to be here."),
        ]
    }
    known = {"tom": {"Tom Reed"}}
    assert facts.addressed(transcript, known) == {
        "Unknown voice 2": ("Tom Reed", "Tom", "00:04:10")
    }


def test_a_name_that_is_nobody_known_needs_to_be_said_twice():
    transcript = {
        "segments": [
            segment(10, 15, "Scott Bowe", "Hi Gregor."),
            segment(16, 20, "Unknown voice 2", "Hello."),
        ]
    }
    assert facts.addressed(transcript, {}) == {}
    again = {
        "segments": [
            *transcript["segments"],
            segment(60, 65, "Scott Bowe", "Okay, Gregor?"),
            segment(66, 70, "Unknown voice 2", "Yes."),
        ]
    }
    assert facts.addressed(again, {}) == {"Unknown voice 2": ("Gregor", "Gregor", "00:00:10")}


def test_greetings_to_a_room_are_not_names():
    transcript = {
        "segments": [
            segment(0, 5, "A", "Hi Everyone. Hello Folks, thanks all."),
            segment(6, 9, "B", "Hey."),
            segment(10, 12, "A", "Thanks, Everybody."),
            segment(13, 15, "B", "Sure."),
        ]
    }
    assert facts.addressed(transcript, {"everyone": {"Every One"}}) == {}


def test_one_name_for_two_voices_is_no_evidence_for_either():
    transcript = {
        "segments": [
            segment(0, 5, "A", "Hi Tom."),
            segment(6, 9, "B", "Hello."),
            segment(10, 12, "A", "Thanks, Tom."),
            segment(13, 15, "C", "Sure."),
        ]
    }
    assert facts.addressed(transcript, {"tom": {"Tom Reed"}}) == {}


class Person:
    def __init__(self, name, aliases=(), path=""):
        self.name, self.aliases, self.path = name, list(aliases), path


def test_a_typed_name_matches_a_person_exactly_by_link_or_and_a_first_name_alone_matches_no_one():
    reed, lee = Person("Tom Reed", path="People/Tom Reed.md"), Person("Tom Lee")
    ann = Person("Ann Cole", aliases=["Annie"])
    people = [reed, lee, ann]
    assert candidates(people, "tom reed") == [reed]
    assert candidates(people, "Annie") == [ann]
    assert candidates(people, "Ann") == []
    assert candidates(people, "Tom") == []
    assert could_be(people, "Ann") == [ann]
    assert could_be(people, "Tom") == [reed, lee]
    assert could_be(people, "Tom Reed") == []
    assert could_be(people, "Quinn") == []
    assert candidates(people, "Tom", "People/Tom Reed") == [reed]
    assert candidates(people, "Zed Quinn") == []
    assert candidates(people, "Quinn") == []
