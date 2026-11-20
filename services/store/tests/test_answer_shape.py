"""Reading a model's answer: safe repairs, refusals that name the part, schema fallback."""

import json

import httpx
import pytest
from coppermind_store import answer_shape
from coppermind_store.model_provider import ModelProvider, ProviderError
from pydantic import ValidationError

from coppermind.context_config import FeatureAssignment, ModelConnection

CONNECTION = ModelConnection(id="t", name="T", provider="openai")
ASSIGNMENT = FeatureAssignment(model="m")
GOOD = {"suggestions": [{"kind": "filename", "value": "Plan", "reason": "r"}], "unresolved": []}


def _reply(text):
    return httpx.Response(
        200, json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
    )


async def _ask(adapter, *, schema=None, correction=None):
    return await adapter.suggest(CONNECTION, "k", ASSIGNMENT, "JSON", {}, schema, correction)


# Recorded stand-ins for what a faster model sent back.
FENCED = "```json\n" + json.dumps(GOOD) + "\n```"
PROSE = "Here are my suggestions for the note:\n\n" + json.dumps(GOOD) + "\n\nHope that helps!"
MISSING_OPTIONAL = json.dumps(
    {"suggestions": [{"kind": "filename", "value": "Plan", "reason": "r"}]}
)
STRING_FOR_LIST = json.dumps(
    {
        "suggestions": [
            {"kind": "event", "event_ids": "evt-1", "reason": "Names match", "quote": "q"}
        ],
        "unresolved": "Which customer?",
    }
)


@pytest.mark.parametrize(
    ("text", "repair"),
    [(FENCED, "code fence"), (PROSE, "text around")],
)
async def test_a_fence_or_leading_prose_is_unwrapped_and_recorded(text, repair):
    adapter = ModelProvider(httpx.MockTransport(lambda request: _reply(text)))
    answer, _ = await _ask(adapter)
    assert dict(answer) == GOOD
    assert answer.raw == text and any(repair in r for r in answer.repairs)
    result, _ = answer_shape.read_suggestions(answer)
    assert result.suggestions[0].kind == "filename"


def test_a_missing_optional_key_is_empty_and_a_lone_string_becomes_a_list():
    result, repairs = answer_shape.read_suggestions(json.loads(MISSING_OPTIONAL))
    assert result.unresolved == [] and result.suggestions[0].event_ids == []
    result, repairs = answer_shape.read_suggestions(json.loads(STRING_FOR_LIST))
    assert result.suggestions[0].event_ids == ["evt-1"]
    assert result.unresolved == ["Which customer?"]
    assert any("suggestions.0.event_ids" in r for r in repairs)
    assert any(r.startswith("unresolved") for r in repairs)


def test_null_for_an_optional_part_is_left_out():
    data = {"suggestions": [{"kind": "filename", "reason": "r", "quote": None, "event_ids": None}]}
    result, repairs = answer_shape.read_suggestions(data)
    assert result.suggestions[0].quote == "" and len(repairs) == 2


def test_a_missing_summary_part_is_empty_but_a_wrong_kind_is_not_repaired():
    summary, repairs = answer_shape.read_summary({"summary": "Done.", "decisions": None})
    assert summary.text("action_items") == "" and len(repairs) == 2
    with pytest.raises(ValidationError):
        answer_shape.read_summary({"summary": ["a", "b"], "decisions": "", "action_items": ""})


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"decisions": "d", "action_items": "a"}, "is missing"),
        ({"summary": None, "decisions": "d", "action_items": "a"}, "is missing"),
        ({"summary": "  ", "decisions": "d", "action_items": "a"}, "is empty or too short"),
    ],
)
def test_a_summary_with_no_summary_part_is_refused_and_named(answer, reason):
    with pytest.raises(ValidationError) as caught:
        answer_shape.read_summary(answer)
    problems = answer_shape.problems_of(caught.value, "summary")
    # One section, named once: never "summary summary" or "summary.summary".
    assert [(p["path"], p["reason"]) for p in problems] == [("meeting_summary.summary", reason)]
    assert problems[0]["text"] == f"The Summary section {reason}."


def test_a_cut_off_answer_is_not_mistaken_for_the_object_inside_it():
    cut = '{"suggestions": [{"kind": "field", "reason": "x"}'
    assert answer_shape.read_json(cut) == (None, [])
    assert answer_shape.read_json('Sure: {"a": 1} Done.')[0] == {"a": 1}


@pytest.mark.parametrize(
    ("data", "path", "reason"),
    [
        ({"suggestions": [{"kind": "filename"}]}, "suggestions.0.reason", "is missing"),
        ({"suggestions": [{"kind": "bogus", "reason": "r"}]}, "suggestions.0.kind", "allowed"),
        ({"suggestions": "none"}, "suggestions", "should be a list"),
        ({"extra": 1}, answer_shape.UNEXPECTED, "not a part"),
        (
            {"suggestions": [{"kind": "event", "event_ids": [3], "reason": "r"}]},
            "suggestions.0.event_ids.0",
            "should be text",
        ),
    ],
)
def test_what_cannot_be_used_is_still_refused_and_named(data, path, reason):
    with pytest.raises(ValidationError) as caught:
        answer_shape.read_suggestions(data)
    problems = answer_shape.problems_of(caught.value)
    assert any(p["path"] == path and reason in p["reason"] for p in problems)


def test_a_refusal_never_repeats_what_the_model_wrote():
    secret = "PRIVATE-TRANSCRIPT-TEXT"
    with pytest.raises(ValidationError) as caught:
        answer_shape.read_suggestions({"suggestions": [{"kind": secret, "reason": "r"}]})
    assert secret not in json.dumps(answer_shape.problems_of(caught.value))


def test_two_objects_or_no_object_is_not_guessed_at():
    assert answer_shape.read_json('{"a": 1} and then {"b": 2}')[0] is None
    assert answer_shape.read_json("I could not do that.")[0] is None
    assert answer_shape.read_json("[1, 2]")[0] is None


async def test_a_schema_is_sent_when_the_service_takes_one():
    seen = []

    def serve(request):
        seen.append(json.loads(request.content))
        return _reply(json.dumps(GOOD))

    adapter = ModelProvider(httpx.MockTransport(serve))
    await _ask(adapter, schema=answer_shape.SUGGESTIONS_SCHEMA)
    sent = seen[0]["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["schema"]["properties"]["suggestions"]
    assert "meeting_summary" not in sent["json_schema"]["schema"]["properties"]


async def test_a_service_that_rejects_a_schema_falls_back_and_is_remembered():
    seen = []

    def serve(request):
        body = json.loads(request.content)
        seen.append(body)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "response_format not supported"})
        return _reply(json.dumps(GOOD))

    adapter = ModelProvider(httpx.MockTransport(serve))
    await _ask(adapter, schema=answer_shape.SUGGESTIONS_SCHEMA)
    # The reasoning parameter is taken away first, then the schema; only the schema is blamed.
    assert [(b["response_format"]["type"], "reasoning_effort" in b) for b in seen] == [
        ("json_schema", True),
        ("json_schema", False),
        ("json_object", False),
    ]
    seen.clear()
    await _ask(adapter, schema=answer_shape.SUGGESTIONS_SCHEMA)
    assert [(b["response_format"]["type"], "reasoning_effort" in b) for b in seen] == [
        ("json_object", True)
    ]


@pytest.mark.parametrize(
    ("said", "sends"),
    [
        ({"error": {"message": "This model's maximum context length is 8192 tokens"}}, 1),
        ({"error": "bad request"}, 4),
    ],
)
async def test_only_a_refusal_not_about_length_walks_the_format_fallbacks(said, sends):
    seen = []

    def serve(request):
        seen.append(json.loads(request.content))
        return httpx.Response(400, json=said)

    adapter = ModelProvider(httpx.MockTransport(serve))
    with pytest.raises(ProviderError):
        await _ask(adapter, schema=answer_shape.SUGGESTIONS_SCHEMA)
    assert len(seen) == sends


async def test_a_service_that_takes_no_reply_format_gets_the_prompt_alone():
    seen = []

    def serve(request):
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(422)
        return _reply(json.dumps(GOOD))

    adapter = ModelProvider(httpx.MockTransport(serve))
    await _ask(adapter, schema=answer_shape.SUGGESTIONS_SCHEMA)
    assert "response_format" not in seen[-1]
    seen.clear()
    await _ask(adapter, schema=answer_shape.SUGGESTIONS_SCHEMA)
    assert len(seen) == 1 and "response_format" not in seen[0]


async def test_a_correction_sends_the_earlier_answer_and_what_was_wrong():
    seen = []

    def serve(request):
        seen.append(json.loads(request.content))
        return _reply(json.dumps(GOOD))

    adapter = ModelProvider(httpx.MockTransport(serve))
    await _ask(adapter, correction=("{bad}", "Your previous answer could not be used"))
    roles = [m["role"] for m in seen[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert seen[0]["messages"][2]["content"] == "{bad}"


async def test_an_answer_that_is_not_json_keeps_the_text():
    adapter = ModelProvider(httpx.MockTransport(lambda request: _reply("sorry, no")))
    with pytest.raises(ProviderError) as caught:
        await _ask(adapter)
    assert caught.value.rule == "provider-not-json" and caught.value.reply == "sorry, no"


def _plain(data, stage=""):
    reader = answer_shape.read_summary if stage else answer_shape.read_suggestions
    with pytest.raises(ValidationError) as caught:
        reader(data)
    return answer_shape.problems_of(caught.value, stage)


@pytest.mark.parametrize(
    ("data", "sentence"),
    [
        (
            {"suggestions": [{"kind": "filename", "reason": ["x"]}]},
            "In the first suggestion, the reason should be text.",
        ),
        (
            {"suggestions": [{"kind": "filename", "reason": "r"}, {"kind": "field"}]},
            "In the second suggestion, the reason is missing.",
        ),
        (
            {"suggestions": [{"kind": "event", "event_ids": [3], "reason": "r"}]},
            "In the first suggestion, the list of meetings should be text.",
        ),
        ({"suggestions": "x"}, "The list of suggestions should be a list."),
    ],
)
def test_each_problem_is_a_plain_sentence_with_no_path_or_index(data, sentence):
    (problem,) = _plain(data)
    assert problem["text"] == sentence
    assert "." not in problem["text"].rstrip(".") and "suggestions." not in problem["text"]


@pytest.mark.parametrize(
    ("data", "sentence"),
    [
        ({"summary": "s", "decisions": ["a"], "action_items": ""}, "The Decisions section "),
        ({"summary": 42, "decisions": "", "action_items": ""}, "The Summary section "),
        ({"decisions": "d", "action_items": "a"}, "The Summary section is missing."),
    ],
)
def test_a_summary_problem_names_the_section_once(data, sentence):
    (problem,) = _plain(data, "summary")
    assert problem["text"].startswith(sentence)
    assert problem["text"].lower().count("summary") <= 1 or "Summary section" in problem["text"]
    assert "summary summary" not in problem["text"].lower()


def test_an_unexpected_key_is_never_kept_in_a_path_or_a_sentence():
    secret = "PRIVATE-TRANSCRIPT-TEXT\nforged log line"
    top = _plain({secret: "x"})
    inner = _plain({"suggestions": [{"kind": "filename", "reason": "r", secret: 1}]})
    summary = _plain({"summary": "s", "decisions": "", "action_items": "", secret: "x"}, "summary")
    for problem in (*top, *inner, *summary):
        kept = json.dumps(problem)
        assert "PRIVATE" not in kept and "forged" not in kept and "\\n" not in kept
        assert "\n" not in problem["path"]
    assert top[0]["path"] == answer_shape.UNEXPECTED
    assert inner[0]["path"] == f"suggestions.0.{answer_shape.UNEXPECTED}"
    assert (
        inner[0]["text"] == "In the first suggestion, there is a part that Coppermind does not use."
    )
    error = answer_shape.AnswerShapeError(top, secret, [])
    assert "PRIVATE" not in str(error) and "PRIVATE" not in error.plain
    assert "PRIVATE" not in answer_shape.feedback(top)
