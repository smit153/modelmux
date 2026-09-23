"""Structured output: ``response_format`` json_object / json_schema (plan 8.4)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from modelmux.api.schemas import ChatCompletionRequest
from modelmux.core.tools import first_json_object, strip_fence
from modelmux.drivers.events import sanitize_detail
from modelmux.errors import InvalidRequestError

ERROR_LIMIT = 300
_SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SUBSCHEMA_SINGLE = ("items", "not", "if", "then", "else", "additionalItems")


@dataclass(frozen=True)
class FormatPolicy:
    kind: Literal["json_object", "json_schema"]
    schema: dict[str, Any] | None
    name: str | None
    validator: Draft202012Validator | None


class FormatValidationError(Exception):
    """The output does not match the format. The message goes into the repair prompt."""


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``schema`` where every object schema forbids unlisted properties.

    Used for ``strict: true``; explicit ``additionalProperties`` are kept.
    """
    result = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        is_object = node.get("type") == "object" or "properties" in node
        if is_object and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for key in _SUBSCHEMA_LISTS:
            for child in node.get(key) or []:
                walk(child)
        for key in _SUBSCHEMA_MAPS:
            for child in (node.get(key) or {}).values():
                walk(child)
        for key in _SUBSCHEMA_SINGLE:
            walk(node.get(key))
        walk(node.get("additionalProperties"))

    walk(result)
    return result


def build_format(req: ChatCompletionRequest) -> FormatPolicy | None:
    """Validate ``response_format``. None for plain text."""
    fmt = req.response_format
    if fmt is None or fmt.type == "text":
        return None
    if fmt.type == "json_object":
        return FormatPolicy("json_object", None, None, None)
    spec = fmt.json_schema
    if spec is None or spec.schema_ is None:
        raise InvalidRequestError(
            message="response_format.json_schema.schema is required.",
            param="response_format.json_schema",
        )
    schema = strictify(spec.schema_) if spec.strict else spec.schema_
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise InvalidRequestError(
            message="response_format.json_schema.schema is not a valid JSON Schema.",
            param="response_format.json_schema.schema",
        ) from None
    return FormatPolicy("json_schema", schema, spec.name, Draft202012Validator(schema))


def format_instructions(policy: FormatPolicy) -> str:
    if policy.kind == "json_object":
        return (
            "Reply with a single valid JSON object only - no prose, no code fences, "
            "nothing before or after it."
        )
    schema = json.dumps(policy.schema, ensure_ascii=False, separators=(",", ":"))
    return (
        "Reply with a single JSON value that validates against this JSON Schema - no prose, "
        "no code fences, nothing before or after it.\n"
        f"Schema: {schema}"
    )


def validate_output(text: str, policy: FormatPolicy) -> str:
    """Return the JSON text (fence removed) or raise ``FormatValidationError``."""
    body, _fenced = strip_fence(text)
    try:
        data = first_json_object(body) if body.startswith("{") else json.loads(body)
    except (ValueError, RecursionError):
        raise FormatValidationError("The reply is not valid JSON.") from None
    if policy.kind == "json_object":
        if not isinstance(data, dict):
            raise FormatValidationError("The reply must be a JSON object.")
    elif policy.validator is not None:
        error = next(iter(policy.validator.iter_errors(data)), None)
        if error is not None:
            path = "/".join(str(p) for p in error.absolute_path) or "(root)"
            message = f"The reply does not match the schema at {path}: {error.message}"
            raise FormatValidationError(sanitize_detail(message, ERROR_LIMIT))
    return json.dumps(data, ensure_ascii=False)
