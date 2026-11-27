"""What the note looks like and what is suggested for it, whatever the model returns."""

from typing import Any

import pytest
from coppermind_store.notes import _renamed_bytes
from coppermind_store.recording_writeup import split_suggestions

from coppermind.recording_writeup import (
    DEFAULT_PROMPT,
    PREVIOUS_DEFAULT_PROMPTS,
    SUGGESTION_END,
    SUGGESTION_START,
    calendar_status,
    compose_body,
    effective_prompt,
    has_references_block,
    read_field_suggestions,
    references_block,
)

VOCAB: dict[str, Any] = {
    "accounts": {"halcyon systems": "Halcyon Systems", "halcyon": "Halcyon Systems"},
    "tags": {"orbit": "orbit", "obs": "orbit"},
    "types": ["work", "personal", "reference"],
}


def read(items, **overrides):
    import json

    return read_field_suggestions(json.dumps({"suggestions": items}), **{**VOCAB, **overrides})


def test_speakers_come_first_then_the_top_line_and_no_title_or_owned_sections():
    body = (
        "Customer meeting (inferred)\n\n## References\nold\n\n## Details\nOrbit 7.2.\n\n"
        "## Speakers\n- the model's own list\n\n## Top line\nHalcyon plans 8.0.\n"
    )
    section = "## Speakers\n\n- Alex Example, 5 min. Identified.\n- Unknown voice 1, 2 min.\n"
    shaped = compose_body(body, section, None)
    assert shaped.startswith(section)
    assert (
        shaped.index("## Top line") < shaped.index("Customer meeting") < shaped.index("## Details")
    )
    assert "References" not in shaped and "the model's own list" not in shaped
    assert not any(line.startswith("# ") for line in shaped.splitlines())


def test_a_clean_recording_prints_no_boundaries_even_when_the_model_wrote_one():
    body = "## Top line\nDone.\n\n## Recording boundaries\n[]\n\n## Notes\nBody.\n"
    assert "boundaries" not in compose_body(body, "", None).lower()


def test_a_boundary_notice_sits_right_after_the_top_line():
    body = "## Top line\nDone.\n\n## Notes\nBody.\n"
    shaped = compose_body(
        body, "## Speakers\n\n- A\n", "## Recording boundaries\n\n- 00:30:00 to 00:40:00: tail.\n"
    )
    assert (
        shaped.index("## Top line")
        < shaped.index("## Recording boundaries")
        < shaped.index("## Notes")
    )


def test_a_prompt_without_a_top_line_still_gets_speakers_first():
    shaped = compose_body(
        "## Summary\nText.\n", "## Speakers\n\n- A\n", "## Recording boundaries\n\nx\n"
    )
    assert (
        shaped.index("## Speakers")
        < shaped.index("## Recording boundaries")
        < shaped.index("## Summary")
    )


def test_references_are_one_block_at_the_bottom():
    block = references_block(
        transcript="[[Sources/x]]", audio="[[Audio/a.wav]]", calendar="matched"
    )
    assert block == (
        "## References\n\n- Source transcript: [[Sources/x]]\n- Recording: [[Audio/a.wav]]\n"
        "- Calendar: matched\n"
    )
    assert has_references_block("## Top line\nx\n\n" + block)
    assert not has_references_block("# Title\n\nBody\n")
    assert "not matched" in calendar_status({"matched": False, "reason": "no event overlaps"})
    assert calendar_status({"matched": True, "event": {"title": "Weekly sync"}}) == (
        "matched to Weekly sync"
    )


def test_renaming_a_recording_note_leaves_no_title_in_the_body():
    text = "---\nid: x\n---\n## Top line\nx\n\n" + references_block(
        transcript="[[S]]", audio=None, calendar="matched"
    )
    data, body = _renamed_bytes(text.encode(), "New title")
    assert "# New title" not in body and data.decode() == text
    plain, body = _renamed_bytes(b"---\nid: x\n---\nBody\n", "New title")
    assert body.startswith("# New title")


def test_context_is_not_asked_for_and_not_read():
    assert read([{"field": "context", "value": "internal", "confidence": "high"}]) == []


def test_a_known_company_is_answered_by_its_own_name():
    (account,) = read([{"field": "account", "value": "halcyon", "confidence": "high"}])
    assert account["value"] == ["Halcyon Systems"]


def test_a_company_nobody_listed_is_answered_as_it_was_heard():
    (account,) = read([{"field": "account", "value": "Brand New Corp", "confidence": "high"}])
    assert account["value"] == ["Brand New Corp"] and account["confidence"] == 0.9


def test_a_low_confidence_answer_or_an_unknown_type_leaves_the_field_empty():
    assert (
        read(
            [
                {"field": "account", "value": "Halcyon", "confidence": "low"},
                {"field": "type", "value": "elsewhere", "confidence": "high"},
                {"field": "type", "value": "journal", "confidence": "high"},
            ]
        )
        == []
    )


def test_tags_are_spelled_as_listed_and_new_ones_are_accepted():
    (tags,) = read(
        [{"field": "tags", "value": ["OBS", "orbit", "#Made Up", "  "], "confidence": "medium"}]
    )
    assert tags["value"] == ["orbit", "made-up"]


@pytest.mark.parametrize("kind", ["work", "personal"])
def test_work_and_personal_are_both_answered_as_a_type(kind):
    (found,) = read([{"field": "type", "value": kind, "confidence": "high"}])
    assert found["value"] == kind


@pytest.mark.parametrize("text", ["nope", "[1, 2", '{"suggestions": 4}'])
def test_an_unreadable_block_is_an_error_not_a_guess(text):
    with pytest.raises(ValueError):
        read_field_suggestions(text, **VOCAB)


def test_the_suggestion_block_is_cut_out_wherever_it_sits():
    block = f"{SUGGESTION_START}\n{{}}\n{SUGGESTION_END}"
    for document in (
        f"# T\n\nBody\n\n{block}\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>",
        f"# T\n\nBody\n\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n{block}",
    ):
        cut, found = split_suggestions(document)
        assert SUGGESTION_START not in cut and found.strip() == "{}"
        assert "<<<RECORDING_BOUNDARIES>>>" in cut
    unclosed, found = split_suggestions(
        f"# T\n\nBody\n\n{SUGGESTION_START}\n{{\n"
        "<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>"
    )
    assert "<<<RECORDING_BOUNDARIES>>>" in unclosed and SUGGESTION_START not in unclosed
    assert split_suggestions("# T\n\nBody") == ("# T\n\nBody", None)


def test_the_default_changes_only_for_owners_who_never_edited_it():
    assert effective_prompt("") == DEFAULT_PROMPT
    assert effective_prompt(PREVIOUS_DEFAULT_PROMPTS[0] + "\n") == DEFAULT_PROMPT
    assert effective_prompt("Write a concise account.") == "Write a concise account."
    assert chr(0x2014) not in DEFAULT_PROMPT


def test_a_default_saved_from_a_browser_with_crlf_line_endings_still_follows_the_default():
    for shipped in (DEFAULT_PROMPT, PREVIOUS_DEFAULT_PROMPTS[0]):
        assert effective_prompt(shipped.replace("\n", "\r\n")) == DEFAULT_PROMPT
    assert effective_prompt("Keep it short.\r\nAdd commitments.") == (
        "Keep it short.\nAdd commitments."
    )
