"""The Speakers section: what it says, and reading back what the owner typed into it."""

from coppermind import speakers_section as sections
from coppermind.speakers_section import Heard


def section(**kwargs):
    return sections.render(
        [
            Heard(
                "Unknown voice 7",
                500,
                quotes=(("We run vSphere 7 on the older cluster.", "00:03:12"),),
                suggestion="Tom Reed, addressed as Tom at 00:04:12",
            ),
            Heard(
                "Scott Bowe",
                1400,
                name="Scott Bowe",
                link="People/Scott Bowe",
                owner=True,
                how="Identified by voice (strong match).",
            ),
            Heard("Unknown voice 8", 60, quotes=()),
        ],
        owner_known=kwargs.get("owner_known", True),
    )


def test_the_owner_is_first_then_named_people_then_unknown_voices_by_talk_time():
    text = section()
    heads = [line for line in text.splitlines() if line.startswith("- ")]
    assert heads == [
        "- [[People/Scott Bowe|Scott Bowe]] (you), 23 min. Identified by voice (strong match).",
        "- Unknown voice 7, 8 min 20 s. Not identified yet.",
        "- Unknown voice 8, 1 min. Not identified yet.",
    ] or heads[0].startswith("- [[People/Scott Bowe|Scott Bowe]] (you), 23 min")
    assert text.index("Scott Bowe") < text.index("Unknown voice 7") < text.index("Unknown voice 8")


def type_name(body, label, name, *, me=False):
    head, mark, rest = body.partition(f"- {label},")
    rest = rest.replace("Name this voice: ", f"Name this voice: {name} ", 1)
    if me:
        rest = rest.replace("[ ] This is me", "[x] This is me", 1)
    return head + mark + rest


def test_an_unidentified_voice_shows_quotes_a_suggestion_and_a_place_for_a_name():
    text = section()
    assert (
        f'  - Said: "We run vSphere 7 on the older cluster." ([[{sections.TARGET_TOKEN}|00:03:12]])'
        in text
    )
    assert "  - Suggestion, not confirmed: Tom Reed, addressed as Tom at 00:04:12." in text
    assert text.count("  - Name this voice: ") == 2
    assert "This is me" not in text
    assert "This is me" in section(owner_known=False)


def test_a_section_without_unknown_voices_has_no_instruction():
    named = sections.render(
        [Heard("A", 30, name="A", how="Identified by voice (strong match).")], owner_known=True
    )
    assert "To name a voice" not in named and "Name this voice" not in named
    assert sections.render([], owner_known=True) == ""


def test_what_the_owner_typed_is_read_back_by_voice():
    body = type_name(section(), "Unknown voice 7", "Tom Reed")
    assert sections.typed_names(body) == {"Unknown voice 7": "Tom Reed"}
    assert [e.label for e in sections.entries(body)] == ["Unknown voice 7", "Unknown voice 8"]
    ticked = type_name(section(owner_known=False), "Unknown voice 8", "Me", me=True)
    assert [e.label for e in sections.entries(ticked) if e.me] == ["Unknown voice 8"]


def test_a_completed_link_gives_the_page_and_the_name():
    assert sections.read_name("[[People/Tom Reed|Tom]]") == ("Tom", "People/Tom Reed")
    assert sections.read_name("[[People/Tom Reed]]") == ("Tom Reed", "People/Tom Reed")
    assert sections.read_name("  **Tom   Reed** ") == ("Tom Reed", "")


def test_naming_a_voice_replaces_only_its_entry_and_keeps_the_talk_time():
    body = type_name(section(owner_known=False), "Unknown voice 7", "Tom R")
    named = sections.name_voice(
        body, "Unknown voice 7", "Tom R", "Tom Reed", "People/Tom Reed", owner=False
    )
    assert "- [[People/Tom Reed|Tom Reed]], 8 min 20 s. Named by you.\n" in named
    assert "Unknown voice 7" not in named
    assert "- Unknown voice 8, 1 min. Not identified yet." in named
    assert "Scott Bowe" in named


def test_a_problem_is_said_under_the_voice_and_what_was_typed_stays():
    body = type_name(section(), "Unknown voice 7", "Tom")
    once = sections.note_problem(body, "Unknown voice 7", "Tom", "two people match Tom.")
    twice = sections.note_problem(once, "Unknown voice 7", "Tom", "two people match Tom.")
    assert once == twice
    assert (
        "  - Could not name this voice yet: two people match Tom.\n  - Name this voice: Tom" in once
    )


def test_a_voice_named_elsewhere_loses_its_questions_unless_the_owner_typed_something():
    text = "## Speakers\n\n" + sections.INSTRUCTION + "\n\n"
    text += '- Tom Reed, 8 min. Not identified yet.\n  - Said: "x"\n  - Name this voice: \n'
    text += "- Ann Lee, 2 min. Not identified yet.\n  - Name this voice: Ann\n\n## Top line\nHi\n"
    settled = sections.settle(text, "Tom Reed")
    assert "- Tom Reed, 8 min. Named by you in another recording.\n" in settled
    assert "Said:" not in settled
    assert sections.INSTRUCTION in settled
    assert sections.settle(settled, "Ann Lee") == settled
    only = sections.settle(text.replace("Ann", "Zed"), "Tom Reed")
    assert "Zed" in only
    all_named = sections.settle(
        "## Speakers\n\n"
        + sections.INSTRUCTION
        + "\n\n- Tom Reed, 8 min. Not identified yet.\n  - Name this voice: \n\n## Top line\nHi\n",
        "Tom Reed",
    )
    assert sections.INSTRUCTION not in all_named and "Name this voice" not in all_named


def test_naming_the_owner_moves_them_to_the_top_of_the_list():
    body = type_name(section(owner_known=False), "Unknown voice 8", "Scott Bowe", me=True)
    named = sections.name_voice(
        body, "Unknown voice 8", "Scott Bowe", "Scott Bowe", "", owner=True, how="Named by you."
    )
    heads = [line for line in named.splitlines() if line.startswith("- ")]
    assert heads[0] == "- Scott Bowe (you), 1 min. Named by you."
    assert [h.split(",")[0] for h in heads[1:]] == [
        "- [[People/Scott Bowe|Scott Bowe]] (you)",
        "- Unknown voice 7",
    ]


def test_a_recording_merged_into_a_note_with_its_own_speakers_section_is_still_read():
    owners = "## Speakers\n\n- Ann Lee, 5 min. Named by you.\n\n"
    merged = owners + "## Recording write-up\n\n" + section(owner_known=False)
    body = type_name(merged, "Unknown voice 7", "Tom Reed")
    assert sections.typed_names(body) == {"Unknown voice 7": "Tom Reed"}

    named = sections.name_voice(
        body, "Unknown voice 7", "Tom Reed", "Tom Reed", "", owner=False, how="Named by you."
    )
    assert "- Tom Reed, 8 min 20 s. Named by you." in named
    assert named.startswith(owners)

    problem = sections.note_problem(body, "Unknown voice 7", "Tom Reed", "two people match Tom.")
    assert "  - Could not name this voice yet: two people match Tom.\n" in problem

    settled = sections.settle(merged, "Tom Reed")
    assert settled == merged

    me = type_name(merged, "Unknown voice 8", "Scott Bowe", me=True)
    top = sections.name_voice(
        me, "Unknown voice 8", "Scott Bowe", "Scott Bowe", "", owner=True, how="Named by you."
    )
    lines = top.splitlines()
    assert lines[0] == "## Speakers"
    assert lines.index("- Scott Bowe (you), 1 min. Named by you.") > lines.index(
        "## Recording write-up"
    )


def test_the_same_recording_local_label_in_two_recordings_is_never_one_voice():
    local = "Unknown voice 1 (recording-local)"
    first = f"## Speakers\n\n- {local}, 5 min. Not identified yet.\n  - Name this voice: \n\n"
    second = f"## Recording write-up\n\n## Speakers\n\n- {local}, 2 min. Not identified yet.\n"
    body = first + second + "  - Name this voice: Tom Reed\n"
    assert sections.repeated_local(body) == {local}
    assert sections.repeated_local(first) == set()
    assert sections.name_voice(body, local, "", "Ann", "", owner=False) == body
    noted = sections.note_problem(body, local, "Tom Reed", "unclear.")
    assert noted.count("Could not name this voice yet: unclear.") == 1
    assert noted.index("unclear.") > noted.index("2 min")
    assert sections.name_voice(first, local, "", "Ann", "", owner=False).count("- Ann, 5 min") == 1
