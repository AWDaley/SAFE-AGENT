"""Closed validation for the JSON Schema subset emitted by pinned tau tools.

The pinned signatures use objects, scalars, arrays, enums and local references.
Unsupported validation keywords fail closed instead of being silently ignored.
No schema can fetch a URL or execute code. This is not a general JSON Schema API.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any


class ArgumentSchemaError(ValueError):
    pass


_ANNOTATIONS = {
    "$schema",
    "$id",
    "$defs",
    "definitions",
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
}
_VALIDATORS = {
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "anyOf",
    "oneOf",
    "allOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minProperties",
    "maxProperties",
}


def parse_argument_object(value: str) -> dict[str, Any]:
    """Decode nested discovery kwargs, rejecting duplicate keys and nonfinite data."""
    if len(value.encode("utf-8")) > 65536:
        raise ArgumentSchemaError("nested arguments exceed transport bound")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, child in items:
            if key in result:
                raise ArgumentSchemaError("duplicate nested argument key")
            result[key] = child
        return result

    def constant(_: str) -> None:
        raise ArgumentSchemaError("nonfinite nested JSON")

    try:
        result = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, RecursionError, json.JSONDecodeError) as exc:
        raise ArgumentSchemaError("invalid nested arguments") from exc
    if type(result) is not dict:
        raise ArgumentSchemaError("nested arguments must be an object")

    def finite(value: Any, depth: int = 0) -> None:
        if depth > 64:
            raise ArgumentSchemaError("nested arguments exceed depth bound")
        if type(value) is float and not math.isfinite(value):
            raise ArgumentSchemaError("nonfinite nested JSON")
        if isinstance(value, (dict, list)):
            for child in value.values() if isinstance(value, dict) else value:
                finite(child, depth + 1)

    finite(result)
    return result


def validate_arguments(
    value: dict[str, Any],
    schema: dict[str, Any],
    *,
    partial: bool = False,
) -> None:
    """Check exact argument names/types. Partial is only for giving a user a tool."""
    _validate(value, schema, root=schema, depth=0, partial=partial, top=True)


def _validate(
    value: Any,
    schema: Any,
    *,
    root: dict[str, Any],
    depth: int,
    partial: bool = False,
    top: bool = False,
) -> None:
    if depth > 64:
        raise ArgumentSchemaError("schema or argument nesting too deep")
    if schema is True:
        return
    if schema is False or not isinstance(schema, dict):
        raise ArgumentSchemaError("argument rejected by schema")
    if set(schema) - _ANNOTATIONS - _VALIDATORS:
        raise ArgumentSchemaError("unsupported argument schema")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ArgumentSchemaError("only local schema references are allowed")
        target: Any = root
        try:
            for segment in ref[2:].split("/"):
                target = target[segment.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise ArgumentSchemaError("unresolved schema reference") from exc
        _validate(value, target, root=root, depth=depth + 1, partial=partial, top=top)
        remaining = {k: v for k, v in schema.items() if k != "$ref"}
        if remaining:
            _validate(value, remaining, root=root, depth=depth + 1, partial=partial)
        return
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword not in schema:
            continue
        successes = 0
        options = schema[keyword]
        if not isinstance(options, list) or not options:
            raise ArgumentSchemaError("invalid schema alternatives")
        for option in options:
            try:
                _validate(value, option, root=root, depth=depth + 1, partial=partial, top=top)
                successes += 1
            except ArgumentSchemaError:
                pass
        if (
            (keyword == "anyOf" and not successes)
            or (keyword == "oneOf" and successes != 1)
            or (keyword == "allOf" and successes != len(options))
        ):
            raise ArgumentSchemaError("argument does not match schema alternatives")
    kind = schema.get("type")
    if isinstance(kind, list):
        _validate(value, {"anyOf": [{"type": item} for item in kind]}, root=root, depth=depth + 1)
    elif kind is not None:
        matches = {
            "object": type(value) is dict,
            "array": type(value) is list,
            "string": type(value) is str,
            "integer": type(value) is int,
            "number": type(value) is int or (type(value) is float and math.isfinite(value)),
            "boolean": type(value) is bool,
            "null": value is None,
        }
        if kind not in matches or not matches[kind]:
            raise ArgumentSchemaError("argument type mismatch")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ArgumentSchemaError("argument const mismatch")
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        raise ArgumentSchemaError("argument enum mismatch")
    if type(value) is dict:
        properties = schema.get("properties", {})
        if not partial and set(schema.get("required", ())) - value.keys():
            raise ArgumentSchemaError("missing required argument")
        additional = schema.get("additionalProperties", False if top or properties else True)
        for key, child in value.items():
            if type(key) is not str:
                raise ArgumentSchemaError("non-string argument key")
            if key in properties:
                _validate(child, properties[key], root=root, depth=depth + 1)
            elif additional is False:
                raise ArgumentSchemaError("unknown argument")
            else:
                _validate(child, additional, root=root, depth=depth + 1)
        if len(value) < schema.get("minProperties", 0):
            raise ArgumentSchemaError("too few object properties")
        if len(value) > schema.get("maxProperties", 1000000):
            raise ArgumentSchemaError("too many object properties")
    if type(value) is list:
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1000000):
            raise ArgumentSchemaError("array length outside bounds")
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(
            value
        ):
            raise ArgumentSchemaError("array contains duplicates")
        for child in value:
            _validate(child, schema.get("items", True), root=root, depth=depth + 1)
    if type(value) is str:
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1000000):
            raise ArgumentSchemaError("string length outside bounds")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ArgumentSchemaError("string pattern mismatch")
    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise ArgumentSchemaError("nonfinite numeric argument")
        if (
            ("minimum" in schema and value < schema["minimum"])
            or ("maximum" in schema and value > schema["maximum"])
            or ("exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"])
            or ("exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"])
        ):
            raise ArgumentSchemaError("numeric argument outside bounds")
