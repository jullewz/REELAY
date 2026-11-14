"""A small, safe JSONPath subset used for ignore and redaction rules.

Supported syntax:

* ``$``            the root
* ``.key`` / ``['key']``  object member
* ``[0]``          array index
* ``[*]`` / ``.*`` every member or element
* ``..key``        ``key`` at any depth

Paths are compiled into step lists and evaluated without ``eval``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

_TOKEN = re.compile(
    r"""\.\.(?P<deep>[A-Za-z_$][\w$-]*|\*)
      | \.(?P<key>[A-Za-z_$][\w$-]*|\*)
      | \[\s*(?P<index>-?\d+)\s*\]
      | \[\s*(?P<star>\*)\s*\]
      | \[\s*(?P<q>['"])(?P<qkey>(?:(?!(?P=q)).)*)(?P=q)\s*\]""",
    re.VERBOSE,
)

Segment = str | int  # concrete location step


@dataclass(frozen=True, slots=True)
class Step:
    kind: str  # "key" | "index" | "wild" | "deep"
    value: str | int | None = None


@lru_cache(maxsize=512)
def compile_path(path: str) -> tuple[Step, ...]:
    text = path.strip()
    if not text.startswith("$"):
        raise ValueError(f"JSONPath must start with '$': {path!r}")
    pos, steps = 1, []
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise ValueError(f"invalid JSONPath {path!r} at position {pos}")
        if match.group("deep") is not None:
            name = match.group("deep")
            steps.append(Step("deep", None if name == "*" else name))
        elif match.group("key") is not None:
            name = match.group("key")
            steps.append(Step("wild") if name == "*" else Step("key", name))
        elif match.group("index") is not None:
            steps.append(Step("index", int(match.group("index"))))
        elif match.group("star") is not None:
            steps.append(Step("wild"))
        else:
            steps.append(Step("key", match.group("qkey")))
        pos = match.end()
    return tuple(steps)


def _walk_all(node: Any, loc: tuple[Segment, ...]) -> Iterator[tuple[tuple[Segment, ...], Any]]:
    yield loc, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_all(value, (*loc, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_all(value, (*loc, i))


def find(document: Any, path: str) -> list[tuple[tuple[Segment, ...], Any]]:
    """Return ``(location, value)`` for every match of ``path``."""
    current: list[tuple[tuple[Segment, ...], Any]] = [((), document)]
    for step in compile_path(path):
        nxt: list[tuple[tuple[Segment, ...], Any]] = []
        for loc, node in current:
            if step.kind == "key":
                if isinstance(node, dict) and step.value in node:
                    nxt.append(((*loc, step.value), node[step.value]))  # type: ignore[arg-type]
            elif step.kind == "index":
                if isinstance(node, list):
                    idx = int(step.value)  # type: ignore[arg-type]
                    if -len(node) <= idx < len(node):
                        real = idx % len(node)
                        nxt.append(((*loc, real), node[real]))
            elif step.kind == "wild":
                if isinstance(node, dict):
                    nxt.extend(((*loc, k), v) for k, v in node.items())
                elif isinstance(node, list):
                    nxt.extend(((*loc, i), v) for i, v in enumerate(node))
            else:  # deep
                for sub_loc, sub in _walk_all(node, loc):
                    if step.value is None:
                        if sub_loc != loc:
                            nxt.append((sub_loc, sub))
                    elif isinstance(sub, dict) and step.value in sub:
                        nxt.append(((*sub_loc, step.value), sub[step.value]))
        current = nxt
    # de-duplicate while keeping order (".." can reach a node twice)
    seen: set[tuple[Segment, ...]] = set()
    unique = []
    for loc, value in current:
        if loc not in seen:
            seen.add(loc)
            unique.append((loc, value))
    return unique


def location_str(loc: tuple[Segment, ...]) -> str:
    out = ["$"]
    for seg in loc:
        if isinstance(seg, int):
            out.append(f"[{seg}]")
        elif re.fullmatch(r"[A-Za-z_$][\w$-]*", seg):
            out.append(f".{seg}")
        else:
            out.append("['" + seg.replace("'", "\\'") + "']")
    return "".join(out)


def matches(path: str, loc: tuple[Segment, ...]) -> bool:
    """Does the concrete location ``loc`` match the pattern ``path``?"""
    steps = compile_path(path)

    def rec(si: int, li: int) -> bool:
        if si == len(steps):
            return li == len(loc)
        step = steps[si]
        if step.kind == "deep":
            for start in range(li, len(loc)):
                seg = loc[start]
                if (step.value is None or seg == step.value) and rec(si + 1, start + 1):
                    return True
            return False
        if li == len(loc):
            return False
        seg = loc[li]
        if step.kind == "wild":
            return rec(si + 1, li + 1)
        if step.kind == "index":
            return isinstance(seg, int) and seg == step.value and rec(si + 1, li + 1)
        return isinstance(seg, str) and seg == step.value and rec(si + 1, li + 1)

    return rec(0, 0)


def delete(document: Any, path: str) -> Any:
    """Remove every match of ``path`` in place. Returns the document."""

    def order(item: tuple[tuple[Segment, ...], Any]) -> tuple[int, list[tuple[int, str]]]:
        loc = item[0]
        return len(loc), [(0, f"{s:020d}") if isinstance(s, int) else (1, s) for s in loc]

    # deepest and highest indices first, so earlier deletions never shift later targets
    for loc, _ in sorted(find(document, path), key=order, reverse=True):
        if not loc:
            continue
        parent = document
        for seg in loc[:-1]:
            parent = parent[seg]
        last = loc[-1]
        if isinstance(parent, dict):
            parent.pop(last, None)
        elif isinstance(parent, list) and isinstance(last, int) and last < len(parent):
            parent.pop(last)
    return document


def assign(document: Any, path: str, value: Any) -> int:
    """Set every match of ``path`` to ``value`` in place. Returns the number of replacements."""
    count = 0
    for loc, _ in find(document, path):
        if not loc:
            continue
        parent = document
        for seg in loc[:-1]:
            parent = parent[seg]
        parent[loc[-1]] = value
        count += 1
    return count
