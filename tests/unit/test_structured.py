from __future__ import annotations

import json
from typing import Any

import pytest

from modelmux.api.schemas import ChatCompletionRequest
from modelmux.core.structured import (
    FormatPolicy,
    FormatValidationError,
    build_format,
    format_instructions,
    strictify,
    validate_output,
)
from modelmux.errors import InvalidRequestError

PERSON = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
        "address": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
    "required": ["name", "age"],
}


def fmt(response_format: dict[str, Any] | None) -> FormatPolicy | None:
    body: dict[str, Any] = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    if response_format is not None:
        body["response_format"] = response_format
    return build_format(ChatCompletionRequest.model_validate(body))


def schema_format(strict: bool = False, schema: dict[str, Any] = PERSON) -> FormatPolicy:
    spec = {"name": "person", "schema": schema, "strict": strict}
    policy = fmt({"type": "json_schema", "json_schema": spec})
    assert policy is not None
    return policy


def test_text_and_absent() -> None:
    assert fmt(None) is None
    assert fmt({"type": "text"}) is None


def test_json_object() -> None:
    policy = fmt({"type": "json_object"})
    assert policy is not None
    assert "single valid JSON object" in format_instructions(policy)
    assert json.loads(validate_output('{"a": 1}', policy)) == {"a": 1}
    assert json.loads(validate_output('```json\n{"a": 1}\n```', policy)) == {"a": 1}
    with pytest.raises(FormatValidationError, match="must be a JSON object"):
        validate_output("[1, 2]", policy)
    with pytest.raises(FormatValidationError, match="not valid JSON"):
        validate_output("Sure! Here you go.", policy)


def test_json_schema_ok() -> None:
    policy = schema_format()
    assert '"required":["name","age"]' in format_instructions(policy)
    out = validate_output('{"name": "Ada", "age": 36, "extra": true}', policy)
    assert json.loads(out)["extra"] is True  # non-strict allows extras


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('{"name": "Ada"}', "'age' is a required property"),
        ('{"name": "Ada", "age": -1}', "at age"),
        ('{"name": "Ada", "age": "36"}', "is not of type 'integer'"),
        ("not json", "not valid JSON"),
    ],
)
def test_json_schema_errors(text: str, error: str) -> None:
    with pytest.raises(FormatValidationError, match=error):
        validate_output(text, schema_format())


def test_strict_rejects_additional_properties() -> None:
    policy = schema_format(strict=True)
    with pytest.raises(FormatValidationError, match="Additional properties"):
        validate_output('{"name": "Ada", "age": 36, "extra": 1}', policy)
    with pytest.raises(FormatValidationError, match="Additional properties"):
        validate_output('{"name": "Ada", "age": 36, "address": {"city": "x", "zip": 1}}', policy)
    validate_output('{"name": "Ada", "age": 36, "address": {"city": "x"}}', policy)


def test_strictify_keeps_explicit_and_recurses() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "object", "additionalProperties": True},
                       "b": {"type": "array", "items": {"type": "object", "properties": {}}}},
        "$defs": {"d": {"properties": {"x": {}}}},
        "anyOf": [{"type": "object"}],
    }  # fmt: skip
    strict = strictify(schema)
    assert strict["additionalProperties"] is False
    assert strict["properties"]["a"]["additionalProperties"] is True
    assert strict["properties"]["b"]["items"]["additionalProperties"] is False
    assert strict["$defs"]["d"]["additionalProperties"] is False
    assert strict["anyOf"][0]["additionalProperties"] is False
    assert "additionalProperties" not in schema  # original untouched


@pytest.mark.parametrize(
    ("response_format", "param"),
    [
        ({"type": "json_schema"}, "response_format.json_schema"),
        ({"type": "json_schema", "json_schema": {"name": "x"}}, "response_format.json_schema"),
        ({"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": 5}}},
         "response_format.json_schema.schema"),
    ],
)  # fmt: skip
def test_invalid_formats(response_format: dict[str, Any], param: str) -> None:
    with pytest.raises(InvalidRequestError) as info:
        fmt(response_format)
    assert info.value.param == param


def test_deep_nesting_is_a_validation_error() -> None:
    with pytest.raises(FormatValidationError):
        validate_output("[" * 100_000 + "]" * 100_000, schema_format())
