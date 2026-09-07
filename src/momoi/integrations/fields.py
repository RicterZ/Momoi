"""Declarative provider fields shared by configuration loading and the dashboard."""

import copy
import json
import math
import os

from ..config.models import ConfigError


def validate_schema(schema):
    if not isinstance(schema, dict):
        raise ValueError("provider fields must be a mapping")
    for name, spec in schema.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("provider fields require names and specifications")
        allowed = {
            "type",
            "default",
            "label",
            "description",
            "required",
            "secret",
            "advanced",
            "enum",
            "minimum",
            "maximum",
            "properties",
            "items",
        }
        if set(spec) - allowed:
            raise ValueError(f"unknown field metadata: {name}")
        for flag in ("required", "secret", "advanced"):
            if flag in spec and type(spec[flag]) is not bool:
                raise ValueError(f"{name}.{flag} must be boolean")
        for text_key in ("label", "description"):
            if text_key in spec and not isinstance(spec[text_key], str):
                raise ValueError(f"{name}.{text_key} must be a string")
        if spec.get("type") not in {
            "string",
            "number",
            "integer",
            "boolean",
            "object",
            "array",
        }:
            raise ValueError(f"unsupported field type: {name}")
        if spec.get("secret") and spec["type"] != "string":
            raise ValueError(f"secret fields must be strings: {name}")
        if spec.get("secret") and (
            spec.get("default") not in (None, "") or "enum" in spec
        ):
            raise ValueError(
                f"secret values must not appear in public field metadata: {name}"
            )
        for key, kind in (("properties", "object"), ("items", "array")):
            if key in spec and spec["type"] != kind:
                raise ValueError(f"{name}.{key} requires {kind}")
        for bound in ("minimum", "maximum"):
            if bound in spec and (
                spec["type"] not in {"number", "integer"}
                or type(spec[bound]) not in (int, float)
                or not math.isfinite(spec[bound])
            ):
                raise ValueError(f"{name}.{bound} requires a finite numeric bound")
        if (
            "minimum" in spec
            and "maximum" in spec
            and spec["minimum"] > spec["maximum"]
        ):
            raise ValueError(f"invalid range: {name}")
        if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
            raise ValueError(f"enum must contain choices: {name}")
        if spec["type"] == "object" and "properties" in spec:
            validate_schema(spec["properties"])
        if spec["type"] == "array":
            validate_schema({"items": spec.get("items")})
        if "enum" in spec:
            if spec["type"] not in {"string", "number", "integer", "boolean"}:
                raise ValueError(f"enum requires a scalar field: {name}")
            for choice in spec["enum"]:
                normalize_value(spec, choice, name, True)
        if "default" in spec:
            if redact_value(spec, spec["default"]) != spec["default"]:
                raise ValueError(
                    f"secret values must not appear in public defaults: {name}"
                )
            normalize_fields({name: spec}, {name: spec["default"]})
    try:
        json.dumps(schema, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("provider schema must be JSON serializable") from None


def normalize_fields(schema, values, *, path="options", enabled=True):
    """Apply defaults and validate values without including secrets in errors.

    Disabled bindings may be incomplete, but still reject malformed supplied values.
    Free-form objects omit properties and are validated by the adapter if needed.
    """
    if not isinstance(values, dict):
        raise ConfigError(f"{path} must be an object")
    if unknown := values.keys() - schema.keys():
        raise ConfigError(f"unknown provider field: {path}.{sorted(unknown)[0]}")
    result = {}
    for name, spec in schema.items():
        location = f"{path}.{name}"
        if name not in values:
            if "default" in spec:
                value = copy.deepcopy(spec["default"])
            elif enabled and spec.get("required"):
                raise ConfigError(f"{location} is required")
            else:
                continue
        else:
            value = copy.deepcopy(values[name])
        result[name] = normalize_value(spec, value, location, enabled)
    return result


def normalize_value(spec, value, path, enabled):
    kind = spec["type"]
    if spec.get("secret") and isinstance(value, dict) and set(value) == {"env"}:
        if not isinstance(value["env"], str) or not value["env"]:
            raise ConfigError(f"{path}.env must name an environment variable")
        value = os.environ.get(value["env"], "")
        if enabled and not value:
            raise ConfigError(f"missing credential environment variable: {path}")
    valid = {
        "string": isinstance(value, str),
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }[kind]
    if not valid:
        raise ConfigError(f"{path} must be {kind}")
    if kind in {"integer", "number"}:
        if not math.isfinite(value):
            raise ConfigError(f"{path} must be finite")
        if ("minimum" in spec and value < spec["minimum"]) or (
            "maximum" in spec and value > spec["maximum"]
        ):
            raise ConfigError(f"{path} is outside the allowed range")
    if kind == "string" and enabled and spec.get("required") and not value.strip():
        raise ConfigError(f"{path} is required")
    if "enum" in spec and value not in spec["enum"]:
        raise ConfigError(f"{path} must be one of the declared choices")
    if kind == "object" and "properties" in spec:
        return normalize_fields(spec["properties"], value, path=path, enabled=enabled)
    if kind == "array":
        return [
            normalize_value(spec["items"], item, f"{path}[{index}]", enabled)
            for index, item in enumerate(value)
        ]
    return value


def redact_fields(schema, values):
    """Redact by field path, including nested objects and array items."""
    result = copy.deepcopy(values)
    if not isinstance(result, dict):
        return result
    for name, spec in schema.items():
        if name in result:
            result[name] = redact_value(spec, result[name])
    return result


def redact_value(spec, value):
    if spec.get("secret") and value not in ("", None):
        return (
            value
            if isinstance(value, dict) and set(value) == {"env"}
            else {"$secret": "keep"}
        )
    if spec["type"] == "object" and isinstance(value, dict):
        return redact_fields(spec.get("properties", {}), value)
    if spec["type"] == "array" and isinstance(value, list):
        return [redact_value(spec["items"], item) for item in value]
    return value
