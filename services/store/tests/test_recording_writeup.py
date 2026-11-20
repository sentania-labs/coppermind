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
        "# Wrong title\nText",
        "# Planning",
        "# Planning\n## Top line",
        "# Planning\n## Action items\n### Invented Person\nSend the document.",
        "# Planning\n\n# Second title\nText",
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
        captured_at="2026-11-20T08:00:00-06:00",
        created_at="2026-11-21T00:00:00Z",
        frontmatter={},
        title="Uploaded recording",
    )
    title, evidence = await resolve_title(context, job, "A greeting.")
    assert title == expected
    assert evidence["matched"] == (len(candidates) == 1)


def test_required_title_can_contain_an_em_dash_but_body_cannot():
    title = "2026-11-20 Planning \u2014 follow-up"
    assert validate("# " + title + "\nA complete note.", title, []) == "A complete note.\n"
    with pytest.raises(ValueError):
        validate("# " + title + "\nA note \u2014 unfinished.", title, [])
