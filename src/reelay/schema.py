"""JSON Schema inference (draft 2020-12) from example documents.

Schemas are inferred per sample and then merged:

* object properties are the union of keys seen, ``required`` holds keys
  present in *every* sample
* integer and number merge to number
* a value that is sometimes ``null`` becomes ``["string", "null"]``
* string formats are detected when every sample agrees: ``date-time``,
  ``date``, ``uuid``, ``email``, ``uri``, ``ipv4``
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_FORMATS: list[tuple[str, re.Pattern[str]]] = [
    ("date-time", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")),
    ("date", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("uuid", re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")),
    ("uri", re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")),
    ("ipv4", re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")),
]


def _string_format(value: str) -> str | None:
    for name, pattern in _FORMATS:
        if pattern.match(value):
            return name
    return None


def infer(value: Any) -> dict[str, Any]:
    """Schema for a single JSON value."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        fmt = _string_format(value)
        return {"type": "string", "format": fmt} if fmt else {"type": "string"}
    if isinstance(value, list):
        items = merge_all(infer(v) for v in value)
        return {"type": "array", "items": items} if items else {"type": "array"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: infer(v) for k, v in value.items()},
            "required": sorted(value.keys()),
        }
    return {}


def _types(schema: dict[str, Any]) -> set[str]:
    t = schema.get("type")
    if t is None:
        return set()
    return set(t) if isinstance(t, list) else {t}


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Least general schema accepting everything ``a`` and ``b`` accept."""
    if not a:
        return dict(b)
    if not b:
        return dict(a)
    types = _types(a) | _types(b)
    if {"integer", "number"} <= types:
        types.discard("integer")
    out: dict[str, Any] = {}
    nullable = "null" in types
    concrete = types - {"null"}

    if len(concrete) > 1 or "anyOf" in a or "anyOf" in b:
        # Genuinely different shapes: keep them apart with anyOf.
        variants: dict[str, dict[str, Any]] = {}
        for schema in _split(a) + _split(b):
            variant = next(iter(_types(schema)), "")
            variants[variant] = merge(variants[variant], schema) if variant in variants else schema
        return {"anyOf": [variants[k] for k in sorted(variants)]}

    kind = next(iter(concrete), None)
    if kind is None:
        return {"type": "null"}
    out["type"] = [kind, "null"] if nullable else kind

    if kind == "string":
        fa, fb = a.get("format"), b.get("format")
        if (fa and fa == fb) or (fa and "string" not in _types(b)) or (fb and "string" not in _types(a)):
            out["format"] = fa or fb
    elif kind == "array":
        items = merge(a.get("items", {}), b.get("items", {}))
        if items:
            out["items"] = items
    elif kind == "object":
        pa, pb = a.get("properties", {}), b.get("properties", {})
        a_is_obj, b_is_obj = "object" in _types(a), "object" in _types(b)
        props = {}
        for key in sorted(pa.keys() | pb.keys()):
            props[key] = merge(pa.get(key, {}), pb.get(key, {}))
        out["properties"] = props
        ra = set(a.get("required", [])) if a_is_obj else None
        rb = set(b.get("required", [])) if b_is_obj else None
        required = ra & rb if ra is not None and rb is not None else (ra if ra is not None else rb) or set()
        if required:
            out["required"] = sorted(required)
    return out


def _split(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """One schema per concrete type (``null`` included), for building ``anyOf``."""
    if "anyOf" in schema:
        return list(schema["anyOf"])
    types = _types(schema)
    if len(types) <= 1:
        return [schema]
    out = []
    for t in sorted(types):
        part = {**schema, "type": t}
        if t != "string":
            part.pop("format", None)
        if t != "array":
            part.pop("items", None)
        if t != "object":
            part.pop("properties", None)
            part.pop("required", None)
        out.append(part)
    return out


def merge_all(schemas: Iterable[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for schema in schemas:
        result = merge(result, schema)
    return result


def infer_many(values: Iterable[Any]) -> dict[str, Any]:
    return merge_all(infer(v) for v in values)
