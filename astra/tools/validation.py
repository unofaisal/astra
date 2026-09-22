"""Argument validation for tool calls.

Uses ``jsonschema`` when installed (full JSON Schema); otherwise a small
built-in validator covering the subset LLM tool schemas actually use
(type, required, enum, const, properties, items, min/max, length, pattern,
additionalProperties, anyOf/oneOf).  Errors are phrased so they can be fed
straight back to the model.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

try:  # optional
    import jsonschema  # type: ignore
except Exception:  # pragma: no cover
    jsonschema = None

_MAX_ERRORS = 5


def validate_args(schema: Dict[str, Any], args: Any) -> List[str]:
    """Return a list of human-readable problems ([] == valid)."""
    if not schema:
        return []
    if jsonschema is not None:
        try:
            validator_cls = getattr(jsonschema, "Draft202012Validator", None) or jsonschema.Draft7Validator
            errs = sorted(validator_cls(schema).iter_errors(args), key=lambda e: list(e.absolute_path))
            out = []
            for e in errs[:_MAX_ERRORS]:
                loc = ".".join(str(p) for p in e.absolute_path) or "(arguments)"
                out.append(f"{loc}: {e.message}")
            return out
        except Exception:
            pass  # bad schema for jsonschema → fall back to the lenient built-in
    errors: List[str] = []
    _check(schema, args, "(arguments)", errors)
    return errors[:_MAX_ERRORS]


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _type_ok(t: Any, value: Any) -> bool:
    if isinstance(t, list):
        return any(_type_ok(x, value) for x in t)
    check = _TYPE_CHECKS.get(t)
    return True if check is None else check(value)


def _check(schema: Dict[str, Any], value: Any, path: str, errors: List[str]) -> None:
    if not isinstance(schema, dict) or len(errors) >= _MAX_ERRORS:
        return
    if "anyOf" in schema or "oneOf" in schema:
        alts = schema.get("anyOf") or schema.get("oneOf") or []
        if alts and not any(not _sub_errors(a, value, path) for a in alts):
            errors.append(f"{path}: does not match any allowed shape")
        return
    t = schema.get("type")
    if t is not None and not _type_ok(t, value):
        errors.append(f"{path}: expected {t if isinstance(t, str) else ' or '.join(t)}, got {type(value).__name__}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
            except re.error:
                pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                errors.append(f"{path}: missing required property '{req}'")
        for k, v in value.items():
            if k in props:
                _check(props[k], v, f"{path}.{k}" if path != "(arguments)" else k, errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{k}'")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                _check(item_schema, item, f"{path}[{i}]", errors)


def _sub_errors(schema: Dict[str, Any], value: Any, path: str) -> List[str]:
    errs: List[str] = []
    _check(schema, value, path, errs)
    return errs


def describe_parameters(schema: Dict[str, Any]) -> str:
    """One-line summary of expected parameters, used in error hints."""
    props = (schema or {}).get("properties") or {}
    req = set((schema or {}).get("required") or [])
    if not props:
        return "no parameters"
    parts = []
    for name, spec in props.items():
        t = spec.get("type", "any") if isinstance(spec, dict) else "any"
        parts.append(f"{name}{'*' if name in req else ''}: {t}")
    return ", ".join(parts) + "  (* = required)"
