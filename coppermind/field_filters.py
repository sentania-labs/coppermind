"""Bounded, schema-validated metadata filters shared by listing and search."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coppermind.schema import FrontmatterSchema


class FieldClause(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field: str = Field(min_length=1, max_length=200)
    op: Literal["eq", "contains", "gte", "lte"]
    value: Any


class FieldFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    all: list[FieldClause] = Field(min_length=1, max_length=20)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("filter: duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("filter: numbers must be finite")
    return number


def _invalid_constant(value: str) -> Any:
    raise ValueError("filter: non-finite numbers are not allowed")


def parse_filter(raw: str) -> FieldFilter:
    if len(quote(raw, safe="").encode()) > 8192:
        raise ValueError("filter: exceeds 8 KiB URL-encoded limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
        result = FieldFilter.model_validate(value)
        for clause in result.all:
            if clause.value is None:
                raise ValueError("filter: null is not a filter value")
        return result
    except (ValidationError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(
            'filter: expected {"all": [{"field": name, "op": operator, "value": value}]} '
            "with 1 to 20 clauses"
        ) from exc


def validate_filter(raw: str, schema: FrontmatterSchema) -> FieldFilter:
    result = parse_filter(raw)
    definitions = {key.name: key for key in schema.keys}
    ranges: dict[str, dict[str, str]] = {}
    for clause in result.all:
        definition = definitions.get(clause.field)
        if definition is None:
            raise ValueError(f"filter.{clause.field}: field is not declared in the schema")
        kind, value = definition.kind, clause.value
        operators = (
            {"contains"} if kind == "list" else {"eq", "gte", "lte"} if kind == "date" else {"eq"}
        )
        if clause.op not in operators:
            raise ValueError(f"filter.{clause.field}: operator is not valid for {kind}")
        valid = True
        if kind in {"string", "enum", "date"}:
            valid = isinstance(value, str)
        elif kind == "bool":
            valid = type(value) is bool
        elif kind == "int":
            valid = type(value) is int
        if kind == "enum" and definition.vocabulary:
            valid = valid and value in definition.vocabulary
        if kind == "date" and valid:
            try:
                valid = date.fromisoformat(value).isoformat() == value
            except ValueError:
                valid = False
        if not valid:
            raise ValueError(
                f"filter.{clause.field}: expected a valid {kind} value without coercion"
            )
        if kind == "date" and clause.op in {"gte", "lte"}:
            bounds = ranges.setdefault(clause.field, {})
            previous = bounds.get(clause.op, value)
            bounds[clause.op] = max(previous, value) if clause.op == "gte" else min(previous, value)
    for name, bounds in ranges.items():
        if bounds.get("gte", "") > bounds.get("lte", "9999-12-31"):
            raise ValueError(f"filter.{name}: lower date bound is after upper bound")
    return result


def filter_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def canonical_filter(raw: str) -> list[dict[str, Any]]:
    clauses = [clause.model_dump() for clause in parse_filter(raw).all]
    return sorted(clauses, key=lambda clause: json.dumps(clause, sort_keys=True))
