"""The document contract rejects structural failures without filing a note."""

import json

import httpx
import pytest
from coppermind_store.model_provider import ModelProvider, ProviderError
from coppermind_store.recording_writeup import validate

from coppermind.context_config import FeatureAssignment, ModelConnection


@pytest.mark.parametrize("provider", ["openai", "compatible", "anthropic"])
@pytest.mark.parametrize("failure", ["", "empty", "cut-short"])
async def test_plain_text_transport_retains_finish_checks(provider, failure):
    document = "# 2026-11-20 Planning\n\n## Top line\nThe release is ready.\n"

    def answer(request):
        payload = json.loads(request.content)
        assert "response_format" not in payload
        text = "" if failure == "empty" else document
        if provider == "anthropic":
            return httpx.Response(
                200,
                json={
                    "stop_reason": "max_tokens" if failure == "cut-short" else "end_turn",
                    "content": [{"type": "text", "text": text}],
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if failure == "cut-short" else "stop",
                        "message": {"content": text},
                    }
                ]
            },
        )

    connection = ModelConnection(
        id="synthetic",
        name="Writer",
        provider=provider,
        base_url="http://local.example/v1" if provider == "compatible" else "",
    )
    model = ModelProvider(httpx.MockTransport(answer))
    if failure:
        with pytest.raises(ProviderError):
            await model.write_text(
                connection, "synthetic-key", FeatureAssignment(), "Write Markdown", {}
            )
    else:
        output, _ = await model.write_text(
            connection, "synthetic-key", FeatureAssignment(), "Write Markdown", {}
        )
        assert output == document


@pytest.mark.parametrize(
    "document",
    [
        "",
        "Text with no heading at all and\n\n{not a document",
        "# Planning",
        "# Planning\n## Top line",
        "# Planning\n## Action items\n### Invented Person\nSend the document.",
        "# Planning\nUnknown voice 9 promised a demo.",
        "# Planning\nSpeaker 1 promised a demo.",
        "# Planning\n<script>alert(1)</script>",
        "# Planning\n---\nreviewed: true\n---",
    ],
)
def test_invalid_document_is_rejected(document):
    with pytest.raises(ValueError):
        validate(document, "Planning", ["Alex", "Unknown voice 1"])


def test_exact_title_is_removed_once_and_known_labels_remain():
    body = "## Action items\nAlex: Demo Friday.\nUnknown voice 1: Send notes.\n"
    assert validate("# Planning\n\n" + body, "Planning", ["Alex", "Unknown voice 1"]) == body


@pytest.mark.parametrize(
    "rows",
    [
        [
            {
                "start_seconds": 8,
                "end_seconds": 3,
                "kind": "unrelated_tail",
                "evidence": "bye",
                "confidence": "low",
            }
        ],
        [
            {
                "start_seconds": 1,
                "end_seconds": 99,
                "kind": "possible_second_meeting",
                "evidence": "hello",
                "confidence": "high",
            }
        ],
        [
            {
                "start_seconds": 1,
                "end_seconds": 3,
                "kind": "delete_audio",
                "evidence": "bye",
                "confidence": "low",
            }
        ],
        {"start_seconds": 1},
    ],
)
def test_invalid_boundaries_are_rejected(rows):
    from coppermind_store.recording_writeup import boundaries

    document = (
        "# Planning\n\n## Recording boundaries\nAn uncertain transition.\n"
        "<<<RECORDING_BOUNDARIES>>>\n" + json.dumps(rows) + "\n<<<END_RECORDING_BOUNDARIES>>>"
    )
    with pytest.raises(ValueError):
        boundaries(document, 10)


@pytest.mark.parametrize(
    "candidates,expected",
    [
        (
            [
                {
                    "event_id": "one",
                    "title": "Release review",
                    "start": "2026-11-20T14:00:00Z",
                    "end": "2026-11-20T15:00:00Z",
                }
            ],
            "2026-11-20 Release review",
        ),
        (
            [
                {
                    "event_id": "one",
                    "title": "Release review",
                    "start": "2026-11-20T14:00:00Z",
                    "end": "2026-11-20T15:00:00Z",
                },
                {
                    "event_id": "two",
                    "title": "Other meeting",
                    "start": "2026-11-20T14:00:00Z",
                    "end": "2026-11-20T15:00:00Z",
                },
            ],
            "2026-11-20 Uploaded recording",
        ),
        ([], "2026-11-20 Uploaded recording"),
    ],
)
async def test_title_abstains_on_ambiguous_calendar(candidates, expected):
    from types import SimpleNamespace

    from coppermind_store.recording_writeup import resolve_title

    from coppermind.settings import default_settings

    async def events(note):
        return candidates

    context = SimpleNamespace(
        calendar_candidates=events,
        config=lambda: SimpleNamespace(assignments={"enrichment": FeatureAssignment()}),
        store=SimpleNamespace(
            control=SimpleNamespace(
                settings=default_settings, schema=lambda: SimpleNamespace(role=lambda name: "date")
            )
        ),
    )
    job = SimpleNamespace(
        id="job",
        captured_at="2026-11-20T08:00:00-06:00",
        created_at="2026-11-21T00:00:00Z",
        frontmatter={},
        title="Uploaded recording",
    )
    title, evidence = await resolve_title(context, job, "Welcome to the Release review.")
    assert title == expected
    assert evidence["matched"] == (len(candidates) == 1)


def test_required_title_can_contain_an_em_dash_but_body_cannot():
    title = "2026-11-20 Planning \u2014 follow-up"
    assert validate("# " + title + "\nA complete note.", title, []) == "A complete note.\n"
    with pytest.raises(ValueError):
        validate("# " + title + "\nA note \u2014 unfinished.", title, [])


TITLE = "2026-11-20 Planning"
VOICES = ["Alex", "Unknown voice 1 (recording-local)"]
NO_BOUNDARIES = "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>"
NOTE = f"# {TITLE}\n\n## Top line\nShip Friday.\n\n## Action items\n### Alex\nSend the plan.\n"


def row(**changes):
    return {
        "start_seconds": 5,
        "end_seconds": 8,
        "kind": "unrelated_tail",
        "evidence": "chat",
        "confidence": "low",
        **changes,
    }


def block(rows):
    return f"\n<<<RECORDING_BOUNDARIES>>>\n{json.dumps(rows)}\n<<<END_RECORDING_BOUNDARIES>>>"


# What a model that follows instructions loosely sends back. Each of these was refused on
# main as "could not be used as a complete note"; each is a note once it is tidied.
LOOSE_REPLIES = {
    "code fence round the whole reply": "```markdown\n" + NOTE + NO_BOUNDARIES + "\n```",
    "a sentence and a code fence round the note": "Sure, here is the note:\n\n```markdown\n"
    + NOTE
    + NO_BOUNDARIES
    + "\n```\nHope that helps.",
    "a code fence round the note but not the boundary block": "```md\n"
    + NOTE
    + "```\n"
    + NO_BOUNDARIES,
    "a sentence before the title": "Here is the meeting note:\n\n" + NOTE + NO_BOUNDARIES,
    "em-dashes": NOTE.replace("Friday.", "Friday \u2014 if QA agrees.") + NO_BOUNDARIES,
    "divider lines": NOTE.replace("\n## Action", "\n---\n\n## Action") + NO_BOUNDARIES,
    "a less-than sign": NOTE.replace("Friday.", "Calls <10 minutes, a > b.") + NO_BOUNDARIES,
    "another title line": NOTE.replace(TITLE, "Planning meeting") + NO_BOUNDARIES,
    "a title line left out": NOTE.split("\n", 2)[2] + NO_BOUNDARIES,
    "a second title": NOTE + "\n# Appendix\nMore.\n" + NO_BOUNDARIES,
    "a voice without its qualifier": NOTE.replace("Send the plan.", "Unknown voice 1 sends it.")
    + NO_BOUNDARIES,
    "a bold owner heading": NOTE.replace("### Alex", "### **Alex**") + NO_BOUNDARIES,
    "an owner heading with a note": NOTE.replace("### Alex", "### Alex (owner)") + NO_BOUNDARIES,
    "no boundary block": NOTE,
    "a fenced boundary block": NOTE + block([]).replace("[]", "```json\n[]\n```"),
    "words after the boundary block": NOTE + NO_BOUNDARIES + "\n\nTell me if you want changes.",
    "a boundary past the end": NOTE + block([row(end_seconds=99)]),
    "boundary values in the wrong form": NOTE
    + "\n## Recording boundaries\nA tail.\n"
    + block([row(start_seconds="5", kind="Unrelated tail", confidence="Low")]),
    "boundaries without their section": NOTE + block([row()]),
    "a boundary that cannot be read": NOTE + block([row(), "nonsense"]),
}


@pytest.mark.parametrize("reply", LOOSE_REPLIES.values(), ids=LOOSE_REPLIES.keys())
def test_loose_replies_are_tidied_into_a_note(reply):
    from coppermind_store.recording_writeup import boundaries, tidy

    document, repairs = tidy(reply, TITLE, VOICES, 10)
    prose, ranges = boundaries(document, 10)
    body = validate(prose, TITLE, VOICES)
    assert "Ship" in body or "Top line" in body or "Executive" in body or body.strip()
    assert "\u2014" not in body and "```" not in body and "\n---" not in body
    assert all(r["end_seconds"] <= 10 for r in ranges)


def test_tidy_keeps_what_the_note_says():
    from coppermind_store.recording_writeup import boundaries, tidy

    reply = LOOSE_REPLIES["boundary values in the wrong form"]
    prose, ranges = boundaries(tidy(reply, TITLE, VOICES, 10)[0], 10)
    assert ranges == [row(start_seconds=5.0)]
    assert "Unknown voice 1 (recording-local) sends it." in validate(
        boundaries(tidy(LOOSE_REPLIES["a voice without its qualifier"], TITLE, VOICES, 10)[0], 10)[
            0
        ],
        TITLE,
        VOICES,
    )
    written = tidy(LOOSE_REPLIES["boundaries without their section"], TITLE, VOICES, 10)[0]
    assert "## Recording boundaries" in written and "unrelated tail" in written


@pytest.mark.parametrize(
    "reply",
    [
        NOTE.replace("### Alex", "### Zed") + NO_BOUNDARIES,
        NOTE + "<script>x</script>" + NO_BOUNDARIES,
        f"# {TITLE}\n---\nreviewed: true\n---\nText" + NO_BOUNDARIES,
        NOTE.replace("Send the plan.", "Unknown voice 9 sends it.") + NO_BOUNDARIES,
        NOTE.replace("Send the plan.", "Speaker 1 sends it.") + NO_BOUNDARIES,
        f"# {TITLE}\n\n## Top line\n" + NO_BOUNDARIES,
        "",
    ],
)
def test_tidying_never_lets_an_unsafe_note_through(reply):
    from coppermind_store.recording_writeup import boundaries, tidy

    with pytest.raises(ValueError):
        prose, _ = boundaries(tidy(reply, TITLE, VOICES, 10)[0], 10)
        validate(prose, TITLE, VOICES)


def test_split_transcript_pieces_fit_and_keep_every_word():
    from coppermind_store.recording_writeup import size_of, split_transcript

    segments = [
        {"speaker": "Alex", "start": n * 10, "end": n * 10 + 9, "text": f"turn {n} " * 20}
        for n in range(40)
    ] + [{"speaker": "Alex", "start": 400, "end": 500, "text": "long " * 2000}]
    pieces = split_transcript({"segments": segments}, 1500)
    assert len(pieces) > 10
    assert all(size_of(piece["text"]) <= 1500 for piece in pieces)
    assert "".join(piece["text"] for piece in pieces).count("turn 7 ") == 20
    assert "".join(piece["text"] for piece in pieces).count("long ") == 2000
    assert [piece["first"] for piece in pieces] == sorted(piece["first"] for piece in pieces)


@pytest.mark.parametrize("line", ["***Decision***: ship it", "--- note", "___init___ is private"])
def test_tidy_leaves_a_line_that_only_starts_like_a_divider(line):
    from coppermind_store.recording_writeup import tidy

    reply = NOTE.replace("Ship Friday.", line) + NO_BOUNDARIES
    document, _ = tidy(reply, TITLE, VOICES, 10)
    assert line in document


def test_a_boundary_record_missing_a_time_is_dropped_and_the_note_is_kept():
    from coppermind_store.recording_writeup import boundaries, tidy

    incomplete = {"kind": "unrelated_tail", "evidence": "x", "confidence": "low", "end_seconds": 5}
    reply = NOTE + block([incomplete, row()])
    document, _ = tidy(reply, TITLE, VOICES, 10)
    prose, ranges = boundaries(document, 10)
    assert "Ship Friday." in validate(prose, TITLE, VOICES)
    assert ranges == [row()]
