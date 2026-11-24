"""Reading a model's answer: what is safe to tidy, and how a refusal explains itself.

A model that is asked for JSON often wraps it in a code fence, or says a sentence first, or
leaves out a part that has nothing in it. None of that changes what the answer says, so it is
unwrapped or defaulted here, and every such repair is recorded. Nothing is ever invented: a
part that is missing and needed, or of the wrong kind, still fails, and the failure names the
part and the reason so the job record and Admin can say what was wrong.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from coppermind.meeting_summary import SECTIONS, MeetingSummary
from coppermind_store.enrichment_models import Suggestion, Suggestions

# How much of a refused answer is kept with the job. Never logged in full at info level.
RAW_KEPT_CHARS = 4000
MAX_PROBLEMS = 12
FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```\s*$", re.DOTALL)


class ModelAnswer(dict[str, Any]):
    """The JSON object a model sent, with the text it arrived as and the tidying it needed."""

    raw: str = ""
    repairs: list[str]

    def __init__(self, data: dict[str, Any], raw: str = "", repairs: list[str] | None = None):
        super().__init__(data)
        self.raw = raw
        self.repairs = list(repairs or [])


class AnswerShapeError(ValueError):
    """The model's answer could not be used as the shape it was asked for."""

    rule = "answer-shape"

    def __init__(self, problems: list[dict[str, str]], raw: str, repairs: list[str]) -> None:
        self.problems = problems
        self.raw = raw[:RAW_KEPT_CHARS]
        self.repairs = repairs
        # What the owner reads: whole sentences, no field paths and no rule names.
        self.plain = "The model's answer could not be used. " + " ".join(
            p.get("text", "") for p in problems[:4]
        )
        super().__init__(
            "The model's answer was not in the expected format (answer-shape): " + say(problems)
        )


def say(problems: list[dict[str, str]]) -> str:
    return "; ".join(f"{p['path'] or 'the answer'} {p['reason']}" for p in problems[:5])[:300]


def read_json(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """The one JSON object in `text` and what had to be removed to find it; None if none."""
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    else:
        return (whole, []) if isinstance(whole, dict) else (None, [])
    repairs: list[str] = []
    body = text
    fenced = FENCE.match(text)
    if fenced:
        body = fenced.group(1)
        repairs.append("removed a code fence around the answer")
        try:
            inner = json.loads(body)
        except ValueError:
            pass
        else:
            return (inner, repairs) if isinstance(inner, dict) else (None, [])
    decoder = json.JSONDecoder()
    start = body.find("{")
    if start == -1:
        return None, []
    try:
        found, end = decoder.raw_decode(body, start)
    except ValueError:
        return None, []
    # Only prose may surround the object; a second object means the answer is ambiguous, so
    # it is refused rather than guessed at. An object that starts later than the first brace
    # would be a piece of a cut-off answer, not the answer.
    if not isinstance(found, dict) or "{" in body[end:]:
        return None, []
    return found, [*repairs, "ignored text around the answer"]


def _as_list(value: Any) -> tuple[Any, bool]:
    return ([value], True) if isinstance(value, str) else (value, False)


def tidy_suggestions(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Default empty optional parts and make a lone string a one-item list. Nothing else."""
    repairs: list[str] = []
    out = dict(data)
    for key in ("suggestions", "unresolved"):
        if key in out and out[key] is None:
            del out[key]
            repairs.append(f"{key} was empty and was left out")
    if "unresolved" in out:
        out["unresolved"], changed = _as_list(out["unresolved"])
        if changed:
            repairs.append("unresolved was one text, made a list of one")
    items = out.get("suggestions")
    if isinstance(items, list):
        fixed = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                fixed.append(item)
                continue
            item = dict(item)
            item.pop("origin", None)
            for key in ("confidence", "entity_ids", "field", "event_ids", "contact_ids", "quote"):
                if key in item and item[key] is None:
                    del item[key]
                    repairs.append(f"suggestions.{index}.{key} was empty and was left out")
            for key in ("entity_ids", "event_ids", "contact_ids"):
                if key in item:
                    item[key], changed = _as_list(item[key])
                    if changed:
                        repairs.append(
                            f"suggestions.{index}.{key} was one text, made a list of one"
                        )
            fixed.append(item)
        out["suggestions"] = fixed
    return out, repairs


def tidy_summary(data: dict[str, Any], keys: list[str]) -> tuple[dict[str, Any], list[str]]:
    """An optional part the model left out, or sent as null, is an empty part."""
    out = dict(data)
    repairs = []
    for key in keys:
        if out.get(key) is None:
            if key in out:
                del out[key]
            out[key] = ""
            repairs.append(f"summary.{key} was missing and was left empty")
    return out, repairs


REASONS = {
    "missing": "is missing",
    "string_type": "should be text",
    "list_type": "should be a list",
    "dict_type": "should be an object",
    "model_type": "should be an object",
    "float_type": "should be a number",
    "int_type": "should be a whole number",
    "bool_type": "should be true or false",
    "extra_forbidden": "is not a part this answer has",
    "literal_error": "is not one of the allowed values",
    "greater_than_equal": "is too small",
    "less_than_equal": "is too large",
    "string_too_short": "is empty or too short",
    "string_too_long": "is too long",
    "too_long": "has too many items",
    "too_short": "has too few items",
}


# The only names that may appear in a stored or logged path: the parts of the answer that
# Coppermind asks for. Any other key is the model's own text and is never kept.
KNOWN_PARTS = {*Suggestions.model_fields, *Suggestion.model_fields, "meeting_summary"} | {
    section.key for section in SECTIONS
}
UNEXPECTED = "(unexpected key)"
SECTION_NAMES = {section.key: section.name for section in SECTIONS}
PART_NAMES = {
    "kind": "kind",
    "confidence": "confidence",
    "entity_ids": "list of people or companies",
    "field": "field name",
    "value": "value",
    "event_ids": "list of meetings",
    "contact_ids": "list of contacts",
    "quote": "quote",
    "reason": "reason",
}
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"]
ORDINALS += ["ninth", "tenth"]


def _ordinal(index: int) -> str:
    return ORDINALS[index] if index < len(ORDINALS) else f"number {index + 1}"


def _safe(part: Any) -> str:
    """A path part as it may be kept: a known name or a number, else a fixed placeholder."""
    if isinstance(part, int) and not isinstance(part, bool):
        return str(part)
    return part if isinstance(part, str) and part in KNOWN_PARTS else UNEXPECTED


def _sentence(loc: tuple[Any, ...], kind: str, reason: str, summary: bool) -> str:
    """One whole sentence for the owner, from where the problem is and what it is."""
    if kind == "extra_forbidden":
        if not summary and len(loc) >= 2 and loc[0] == "suggestions" and isinstance(loc[1], int):
            return (
                f"In the {_ordinal(loc[1])} suggestion, there is a part that Coppermind "
                "does not use."
            )
        return "The answer has a part that Coppermind does not use."
    if summary:
        key = loc[0] if loc else ""
        if key in SECTION_NAMES:
            return f"The {SECTION_NAMES[key]} section {reason}."
        return "The summary was not in the expected form."
    if loc and loc[0] == "unresolved":
        return f"The list of open questions {reason}."
    if loc and loc[0] == "suggestions":
        if len(loc) == 1:
            return f"The list of suggestions {reason}."
        if isinstance(loc[1], int):
            who = f"{_ordinal(loc[1])} suggestion"
            part = loc[2] if len(loc) > 2 else None
            if isinstance(part, str) and part in PART_NAMES:
                return f"In the {who}, the {PART_NAMES[part]} {reason}."
            return f"The {who} {reason}."
    return f"The answer {reason}."


def problems_of(exc: ValidationError, stage: str = "") -> list[dict[str, str]]:
    """Each place pydantic refused and why, in words that never repeat the model's input.

    `path` is for support and keeps only known part names and numbers. `text` is the plain
    sentence shown to the owner. `stage` is "summary" for the meeting summary request.
    """
    summary = stage == "summary"
    found: list[dict[str, str]] = []
    for error in exc.errors():
        loc = tuple(error["loc"])
        kind = error["type"]
        if kind == "value_error":
            # Raised by Coppermind's own checks, never by model text.
            reason = " ".join(str(error.get("msg", "")).removeprefix("Value error, ").split())
            reason = reason[:200]
            first = reason.split(" ", 1)[0]
            if summary and not loc and first in SECTION_NAMES:
                # "summary must be text" is about the one section it names.
                loc, reason = (first,), reason[len(first) :].strip()
        else:
            reason = REASONS.get(kind, "is not in the expected form")
        path = ".".join([*(["meeting_summary"] if summary else []), *(_safe(p) for p in loc)])
        problem = {
            "path": path[:120].replace("\n", " "),
            "reason": reason,
            "text": _sentence(loc, kind, reason, summary),
        }
        if problem not in found:
            found.append(problem)
        if len(found) >= MAX_PROBLEMS:
            break
    return found


def not_json(raw: str) -> list[dict[str, str]]:
    return [
        {
            "path": "",
            "reason": "was not a single JSON object (it may be cut off, or hold more than one)",
            "text": "The answer was not a single JSON object, so it could not be read.",
        }
    ]


def feedback(problems: list[dict[str, str]]) -> str:
    """What the model is told when its answer is refused and it is asked once more."""
    lines = "\n".join(
        f"- {p.get('text', '')} (at {p['path'] or 'the answer'})".replace(" (at the answer)", "")
        for p in problems
    )
    return (
        "Your previous answer could not be used for these reasons:\n"
        f"{lines}\n"
        "Reply again with one JSON object only, no code fence and no other text, that "
        "fixes every point above and keeps to the keys and kinds of value in the "
        "instructions."
    )


def _schema() -> dict[str, Any]:
    """The answer the suggestions prompt asks for. The summary is a separate request."""
    schema = Suggestions.model_json_schema()
    schema["properties"].pop("meeting_summary", None)
    schema["$defs"]["Suggestion"]["properties"].pop("origin", None)
    schema.pop("title", None)
    return schema


SUGGESTIONS_SCHEMA = _schema()
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {section.key: {"type": "string"} for section in SECTIONS},
    "required": [section.key for section in SECTIONS],
    "additionalProperties": False,
}


def read_suggestions(answer: dict[str, Any]) -> tuple[Suggestions, list[str]]:
    data, repairs = tidy_suggestions(dict(answer))
    return Suggestions.model_validate(data), repairs


def read_summary(answer: dict[str, Any]) -> tuple[MeetingSummary, list[str]]:
    required = SECTIONS[0].key
    data, repairs = tidy_summary(dict(answer), [section.key for section in SECTIONS[1:]])
    given = data.get(required)
    if given is None or (isinstance(given, str) and not given.strip()):
        raise ValidationError.from_exception_data(
            "MeetingSummary",
            [
                {
                    "type": "missing" if given is None else "string_too_short",
                    "loc": (required,),
                    "input": "",
                    "ctx": {"min_length": 1},
                }
            ],
        )
    return MeetingSummary.from_answer(data), repairs
