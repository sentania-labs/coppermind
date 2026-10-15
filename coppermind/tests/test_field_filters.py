"""Typed filter validation rejects ambiguity before querying the mirror."""

import json

import pytest

from coppermind.field_filters import canonical_filter, parse_filter, validate_filter
from coppermind.schema import FrontmatterSchema, default_schema

SCHEMA = FrontmatterSchema.model_validate(
    {
        "roles": default_schema().model_dump()["roles"],
        "keys": [
            *default_schema().model_dump()["keys"],
            {"name": "name", "kind": "string"},
            {"name": "count", "kind": "int"},
            {"name": "enabled", "kind": "bool"},
            {"name": "day", "kind": "date"},
            {"name": "people", "kind": "list"},
            {"name": "choice", "kind": "enum", "vocabulary": ["a", "b"]},
        ],
    }
)


def raw(field, op, value):
    return json.dumps({"all": [{"field": field, "op": op, "value": value}]})


@pytest.mark.parametrize(
    "field,op,value",
    [
        ("name", "eq", 1),
        ("count", "eq", True),
        ("count", "eq", 1.0),
        ("enabled", "eq", 1),
        ("enabled", "eq", "true"),
        ("day", "eq", "20261015"),
        ("day", "eq", "2026-02-30"),
        ("choice", "eq", "c"),
        ("unknown", "eq", "a"),
        ("people", "eq", "a"),
        ("name", "contains", "a"),
        ("name", "gte", "a"),
    ],
)
def test_invalid_types_and_operators(field, op, value):
    with pytest.raises(ValueError):
        validate_filter(raw(field, op, value), SCHEMA)


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"all":[{"field":"people","op":"contains","value":1e999}]}',
        '{"all":[]}',
        '{"all":[],"all":[]}',
        raw("name", "eq", None),
        raw("people", "contains", [float("nan")]),
        raw("name", "eq", "é" * 2000),
        json.dumps({"all": [{"field": "name", "op": "eq", "value": "x"}] * 21}),
    ],
)
def test_bounded_unambiguous_json(payload):
    with pytest.raises(ValueError):
        parse_filter(payload)


def test_reversed_ranges_and_order_normalization():
    clauses = [
        {"field": "day", "op": "gte", "value": "2026-10-20"},
        {"field": "day", "op": "lte", "value": "2026-10-01"},
    ]
    value = json.dumps({"all": clauses})
    with pytest.raises(ValueError, match="lower date bound"):
        validate_filter(value, SCHEMA)
    assert canonical_filter(value) == canonical_filter(json.dumps({"all": clauses[::-1]}))
