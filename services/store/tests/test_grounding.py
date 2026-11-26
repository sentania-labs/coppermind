"""A write-up is held to what the recording says: what it adds is marked, what it keeps is real.

The transcripts and write-ups under fixtures/grounding are recorded: the reply a model gave for a
made-up customer call (every figure in it is in the transcript, in another form), the reply it
gave for a team weekly that invented an amount, a count and a name, and the refusal a model gave
for a recording it would not write up. No model is called.
"""

import time
from datetime import date
from pathlib import Path

import pytest
from coppermind_store.grounding import MARK, ground, spoken
from coppermind_store.recording_writeup import declines

FIXTURES = Path(__file__).parent / "fixtures" / "grounding"
TODAY = date(2026, 11, 26)
INVITE = ["Acme architecture review", "Dana Whitfield", "Priya Raman", "Marcus Hale", "Scott"]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def marked(note: str, transcript: str, **kwargs) -> list[str]:
    kwargs.setdefault("recorded_on", TODAY)
    return ground(note, transcript, **kwargs)[1]


def test_what_the_weekly_invented_is_marked_where_it_stands():
    note, transcript = fixture("weekly_writeup.md"), fixture("weekly_transcript.txt")
    out, found = ground(note, transcript, known=["Scott", "Marcus Hale"], recorded_on=TODAY)
    assert "a $250,000 (not in the recording) hardware budget" in out
    assert "migrate 400 (not in the recording) hosts" in out
    assert "signed off by Priya Raman (not in the recording)." in out
    assert "Northwind Systems (not in the recording) after the firmware lands" in out
    assert "starts in January (not in the recording)" in out
    assert {"$250,000", "400", "Priya Raman", "Northwind Systems", "January"} <= set(found)
    # What the recording does say is left alone.
    assert (
        "Half the lab hosts are reimaged; the rest wait on firmware (Marcus Hale, [00:00:10])"
        in out
    )
    assert "I help Marcus Hale on Thursday afternoon." in out


def test_the_note_is_only_ever_added_to():
    note, transcript = fixture("weekly_writeup.md"), fixture("weekly_transcript.txt")
    out, found = ground(note, transcript, known=["Scott"], recorded_on=TODAY)
    assert found
    assert out.replace(" " + MARK, "") == note
    assert out.count(MARK) == len(found)


def test_a_note_that_says_only_what_was_said_is_returned_exactly_as_it_was():
    note, transcript = fixture("acme_writeup.md"), fixture("acme_transcript.txt")
    out, found = ground(note, transcript, known=INVITE + ["Unknown voice 1"], recorded_on=TODAY)
    assert found == [] and out == note


def test_marking_twice_marks_once():
    note, transcript = fixture("weekly_writeup.md"), fixture("weekly_transcript.txt")
    once, _ = ground(note, transcript, recorded_on=TODAY)
    again, found = ground(once, transcript, recorded_on=TODAY)
    assert again == once and found == []


HEARD_NUMBERS = [
    # (what was said, what the note says)
    ("we set aside two hundred fifty thousand dollars", "a budget of $250,000"),
    ("we set aside two hundred fifty thousand dollars", "a budget of $250k"),
    ("we set aside 250,000 dollars", "a budget of $250k"),
    ("we set aside $250k", "a budget of 250,000 dollars"),
    ("we set aside 250 thousand", "a budget of $250,000"),
    ("about a hundred and forty hosts", "about 140 hosts"),
    ("one hundred forty hosts", "about 140 hosts"),
    ("roughly twelve hundred VMs", "roughly 1,200 VMs"),
    ("twenty five percent of the budget", "25% of the budget"),
    ("twenty-five per cent of the budget", "25 percent of the budget"),
    ("a contract of two and a half million", "a contract of $2.5M"),
    ("a contract of two point five million dollars", "a contract of $2,500,000"),
    ("a contract of 2.5 million", "a contract of two million five hundred thousand dollars"),
    ("it took forty minutes", "it took 40 minutes"),
    ("it took 40 minutes", "it took forty minutes"),
    ("two thousand five hundred seats", "2,500 seats"),
    ("a thousand seats", "1,000 seats"),
    ("three hundred hosts", "300 hosts"),
    ("1.5 billion events", "$1.5B of events"),
]


@pytest.mark.parametrize(("said", "note"), HEARD_NUMBERS)
def test_a_figure_in_another_form_is_not_marked(said, note):
    transcript = f"[00:00:01] Dana: {said}.\n"
    assert marked(f"## Top line\n- {note}.\n", transcript) == []


@pytest.mark.parametrize(
    "note",
    [
        "a budget of $250,000",
        "a budget of $250k",
        "about 400 hosts",
        "a 25% cut",
        "about 140 hosts",
        "a rise of 12.5 points",
        "four hundred hosts",
        "two hundred fifty thousand dollars",
    ],
)
def test_a_figure_nobody_said_is_marked(note):
    transcript = "[00:00:01] Dana: We run vSphere 7 on forty hosts. Half are reimaged.\n"
    found = marked(f"## Top line\n- {note}.\n", transcript)
    assert len(found) == 1


def test_amounts_carry_over_whichever_way_they_were_said_and_written():
    out, found = ground(
        "- $250k, then $250,000, then 250 thousand, then two hundred fifty thousand\n",
        "[00:00:01] A: The figure is 250,000.\n",
    )
    assert found == [] and MARK not in out


@pytest.mark.parametrize(
    "line",
    [
        "- Met at 3pm and again at 10:30 for 45:00 (see [00:12:40]).",
        "- Two of the three sites moved; 3 of 4 answered.",
        "1. First item of 2.",
        "- Version 8.0.2 and 8.0U2 are installed; the build is v7 or H100.",
        "- Seen on the 5th and 22nd floors.",
    ],
)
def test_clock_times_ordinals_versions_and_small_counts_are_not_figures(line):
    assert marked("## Notes\n" + line + "\n", "[00:00:01] A: hello there.\n") == []


def test_a_model_counting_its_own_items_is_not_marked():
    assert marked("## Top line\nThree commitments came out of this, 2 for me.\n", "hi") == []


def test_spoken_numbers_are_read_whole_and_in_place():
    text = "ninety nine, twenty-one, two thousand twenty six, one two three, a hundred"
    values = [int(value) for value, _, _ in spoken(text)]
    assert values == [99, 21, 2026, 1, 2, 3, 100]
    text = "two and a half million, then four hundred and fifty thousand"
    assert [str(v) for v, _, _ in spoken(text)] == ["2500000.0", "450000"]


NAMED = "[00:00:01] Dana Whitfield: Priya owns storage. Talk to marcus about the Acme account.\n"


@pytest.mark.parametrize(
    "note",
    [
        "- Priya owns the storage side.",
        "- PRIYA owns the storage side.",
        "- Per priya, the storage side is hers.",
        "- Talk to Marcus about the Acme account.",
        "- Acme's account needs a plan, said Dana.",
        "- Dana Whitfield will send it.",
        "- The follow-up is with Dana Whitfield and Priya Raman.",
        "- Ask Marcus Hale (networking) later.",
    ],
)
def test_a_name_in_another_case_or_in_part_is_not_marked(note):
    assert marked("## Notes\n" + note + "\n", NAMED, known=["Marcus Hale"]) == []


def test_the_invite_the_speakers_and_the_glossary_are_known_without_being_said():
    note = (
        "## Notes\n- Raised with Tomasz Kowalczyk and Jordan Lee.\n"
        "- The Quarterly Roadmap review follows. Vexa Labs asked about VMware Cloud Foundation.\n"
    )
    transcript = "[00:00:01] Scott: We spoke earlier.\n"
    assert len(marked(note, transcript)) >= 4
    known = [
        "Quarterly Roadmap review",  # the matched invite's title
        "Tomasz Kowalczyk",  # an attendee
        "Jordan Lee",  # a speaker
        "cloud foundation = VMware Cloud Foundation\nvexa = Vexa Labs",  # the glossary
    ]
    assert marked(note, transcript, known=known) == []


@pytest.mark.parametrize(
    "note",
    [
        "- They signed with Contoso last year.",
        "- I met Marcus Webb about it.",
        "- The vendor, Fabrikam Systems, will bill us.",
        "- (Lena Okafor, [00:31:05])",
    ],
)
def test_a_name_nobody_said_is_marked_after_the_name(note):
    transcript = "[00:00:01] Dana: We run vSphere on the older cluster.\n"
    out, found = ground("## Notes\n" + note + "\n", transcript)
    assert len(found) == 1
    assert out.count(MARK) == 1


def test_only_the_invented_name_is_marked_not_the_words_around_it():
    out, found = ground(
        "## Notes\n- We handed it to Fabrikam Systems on Friday.\n",
        "[00:00:01] Dana: Friday works for the handover.\n",
    )
    assert out == "## Notes\n- We handed it to Fabrikam Systems (not in the recording) on Friday.\n"
    assert found == ["Fabrikam Systems"]


@pytest.mark.parametrize(
    "line",
    [
        "**Next steps**: Send the plan.",
        "The note is in Markdown, OK, per the CEO and the API team.",
        "I'm sure I'll follow up with the IT group.",
        "Unknown voice 3 spoke for 45 minutes.",
        "See [[Acme architecture review]] and `Contoso` at https://contoso.example/Fabrikam.",
        "**Decision**: Ship it.",
    ],
)
def test_ordinary_capitals_labels_links_and_code_are_not_names(line):
    transcript = "[00:00:01] Unknown voice 3: We shipped it, forty five minutes.\n"
    assert marked("## Top line\n- " + line + "\n", transcript) == []


def test_headings_are_not_read():
    note = "# Contoso review 2031\n## Fabrikam $250,000\nBody text.\n"
    assert ground(note, "[00:00:01] A: Body.\n")[0] == note


# -- dates ---------------------------------------------------------------------------------


def test_a_date_worked_out_from_a_relative_one_is_not_marked():
    transcript = (
        "[00:00:01] Scott: I will send it by Friday, and we plan for the end of next year.\n"
    )
    note = (
        "## Top line\n- Send it by Friday, December 4, 2026.\n"
        "- Target of December 2027, with a check on 12/04/2026 and 2026-12-04.\n"
    )
    assert marked(note, transcript) == []


def test_a_date_that_was_said_is_not_marked_and_one_that_was_not_is():
    transcript = "[00:00:01] Scott: The review is on March 15, and then in April.\n"
    assert marked("- Review on March 15, 2027 and again in April.\n", transcript) == []
    assert marked("- Review on March 22.\n", transcript) == ["March 22"]
    assert marked("- Review on 22 March.\n", transcript) == ["22 March"]


def test_a_month_and_day_without_a_year_is_not_split_into_two_figures():
    transcript = "[00:00:01] Scott: We spoke on the call.\n"
    assert marked("- They met on 11/26 and will meet on 3/4.\n", transcript) == []


def test_a_ticket_number_is_marked_once_and_only_when_it_was_not_said():
    transcript = "[00:00:01] Scott: The ticket is PROJ-1234 and it is open.\n"
    assert marked("- Tracked in PROJ-1234.\n", transcript) == []
    assert marked("- Tracked in PROJ-9981.\n", transcript) == ["PROJ-9981"]


def test_a_date_with_no_talk_of_dates_in_the_recording_is_marked():
    transcript = "[00:00:01] Scott: We run vSphere on the older cluster.\n"
    note = "- Cutover on November 27, 2026 and again in March.\n- Contract renews 2031-01-05.\n"
    assert marked(note, transcript) == ["November 27, 2026", "March", "2031-01-05"]


def test_a_far_off_date_is_marked_even_when_the_recording_talks_in_days():
    transcript = "[00:00:01] Scott: I will send it by Friday.\n"
    assert marked("- Send it December 4, 2026.\n", transcript) == []
    assert marked("- Send it March 4, 2027.\n", transcript) == ["March 4, 2027"]
    assert marked("- Send it in 2031.\n", transcript) == ["2031"]


def test_the_recordings_own_day_and_the_word_may_are_not_marked():
    transcript = "[00:00:01] Scott: hello.\n"
    assert marked("- Recorded 2026-11-26, on November 26.\n", transcript) == []
    assert marked("- We may need a second pass; they may ask.\n", transcript) == []


# -- a long recording stays cheap ----------------------------------------------------------


def test_a_long_transcript_costs_no_more_than_its_length():
    words = "the cluster hosts migrate firmware Marcus Dana two hundred forty 250,000 $3k"
    line = "[00:00:01] Dana Whitfield: " + words + ".\n"
    note = (
        "## Top line\n"
        + "- Dana will migrate 250,000 hosts for Fabrikam in March, two hundred forty of them.\n"
        * 3000
    )

    def took(lines: int) -> float:
        start = time.perf_counter()
        ground(note, line * lines, known=["Dana Whitfield"], recorded_on=TODAY)
        return time.perf_counter() - start

    small, large = took(5_000), took(40_000)  # about 0.4 MB and 3.3 MB of transcript
    assert large < 5.0
    assert large < small * 24 + 0.5  # eight times the transcript, nowhere near its square


def test_a_long_note_is_read_in_one_pass():
    note = "## Top line\n" + "- Fabrikam owes $250,000 by January 5, 2031 (Webb Smith).\n" * 20_000
    start = time.perf_counter()
    out, found = ground(note, "[00:00:01] A: nothing here.\n" * 2000, recorded_on=TODAY)
    assert time.perf_counter() - start < 8
    assert len(found) == 3 * 20_000 and out.count(MARK) == 3 * 20_000


# -- a reply that is not a note -------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        fixture("contoso_reply.txt"),
        "I'm sorry, but I can't help with that.",
        "Sorry, I cannot summarize this recording.",
        "I apologize, I was unable to write a note from this transcript.",
        "I am unable to generate the notes you asked for.",
        "As an AI language model I cannot do that.",
        "The transcript is empty, so there is nothing to summarize.",
        "There is no transcript to write a note from.",
        "# 2026-11-20 Planning\n\nI could not produce a note for this recording.",
        "# 2026-11-20 Planning\n\n## Top line\nI can't write this note.",
        "```\nI could not produce a note for this recording.\n```",
        "I'm unable to summarize this recording.",
        "I was not able to write notes.",
        "I cannot access the audio.",
        "Unfortunately, there is not enough audio content to create meeting notes.",
        "There is no meaningful content in this transcript to write up.",
        "# 2026-11-20 Planning\n\nThe call was short and nothing was decided.",
    ],
)
def test_a_reply_that_is_not_a_note_is_recognised(reply):
    assert declines(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "# 2026-11-20 Planning\n\n## Top line\nThe release is ready.",
        "## Top line\nI cannot attend on Friday; Dana will go in my place.",
        "## Top line\nI could not reach Dana, so I will write to her instead.",
        "## Top line\nWe could not provide the budget this quarter.",
        "# 2026-11-20 Planning\n\n## Top line\nUnknown voice 1 said hello.",
        "- Unknown voice 1 said hello.\n- Unknown voice 2 answered.",
        "[00:00:05] Unknown voice 1 said hello.",
        "## Top line\nDecided.\n\n## What I promised\n- I cannot write the spec before March.",
        "## Top line\nSorry came up twice but nobody was.\n- I could not make the call.",
        "## Notes\n" + "x " * 600 + "I could not produce a note for this recording.",
    ],
)
def test_a_note_is_never_mistaken_for_a_refusal(reply):
    assert not declines(reply)


def test_one_enormous_line_is_read_in_one_pass_too():
    note = "- Fabrikam owes $250,000 by January 5, 2031 (Webb Smith); " * 4000
    start = time.perf_counter()
    out, found = ground(note, "[00:00:01] A: nothing here.\n", recorded_on=TODAY)
    assert time.perf_counter() - start < 5
    assert len(found) == 4 * 4000 - 1 and out.count(MARK) == 4 * 4000 - 1
