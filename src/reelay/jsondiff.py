"""Structural JSON diff with JSONPath locations and ignore rules."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from reelay import jsonpath
from reelay.jsonpath import Segment


@dataclass(frozen=True, slots=True)
class Delta:
    op: str  # "added" | "removed" | "changed" | "type"
    path: str
    old: Any = None
    new: Any = None

    def describe(self, limit: int = 60) -> str:
        def short(value: Any) -> str:
            text = json.dumps(value, ensure_ascii=False)
            return text if len(text) <= limit else text[: limit - 1] + "…"

        if self.op == "added":
            return f"+ {self.path} = {short(self.new)}"
        if self.op == "removed":
            return f"- {self.path} (was {short(self.old)})"
        if self.op == "type":
            return f"~ {self.path}: {json_type(self.old)} → {json_type(self.new)}"
        return f"~ {self.path}: {short(self.old)} → {short(self.new)}"

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "path": self.path, "old": self.old, "new": self.new}


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def diff(old: Any, new: Any, ignore: Iterable[str] = (), *, max_deltas: int = 500) -> list[Delta]:
    """Compare two JSON documents. Locations matching any ``ignore`` JSONPath are skipped."""
    patterns = list(ignore)
    for pattern in patterns:
        jsonpath.compile_path(pattern)
    out: list[Delta] = []

    def ignored(loc: tuple[Segment, ...]) -> bool:
        return any(jsonpath.matches(p, loc) for p in patterns)

    def rec(a: Any, b: Any, loc: tuple[Segment, ...]) -> None:
        if len(out) >= max_deltas or ignored(loc):
            return
        ta, tb = json_type(a), json_type(b)
        numeric = {ta, tb} <= {"integer", "number"}
        if ta != tb and not numeric:
            out.append(Delta("type", jsonpath.location_str(loc), a, b))
            return
        if isinstance(a, dict):
            assert isinstance(b, dict)
            for key in a:
                if key not in b:
                    if not ignored((*loc, key)):
                        out.append(Delta("removed", jsonpath.location_str((*loc, key)), old=a[key]))
                else:
                    rec(a[key], b[key], (*loc, key))
            for key in b:
                if key not in a and not ignored((*loc, key)):
                    out.append(Delta("added", jsonpath.location_str((*loc, key)), new=b[key]))
        elif isinstance(a, list):
            assert isinstance(b, list)
            for i in range(max(len(a), len(b))):
                if i >= len(a):
                    if not ignored((*loc, i)):
                        out.append(Delta("added", jsonpath.location_str((*loc, i)), new=b[i]))
                elif i >= len(b):
                    if not ignored((*loc, i)):
                        out.append(Delta("removed", jsonpath.location_str((*loc, i)), old=a[i]))
                else:
                    rec(a[i], b[i], (*loc, i))
        elif a != b:
            out.append(Delta("changed", jsonpath.location_str(loc), a, b))

    rec(old, new, ())
    return out
