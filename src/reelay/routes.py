"""Route patterns: ``"GET /users/*"``, ``"* /admin/**"``, ``"/health"``.

The optional method (or ``*``) is followed by a path glob where ``*`` is
one path segment and ``**`` any number of segments. Several methods can be
given with ``|``: ``"GET|HEAD /static/**"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

_METHODS = re.compile(r"^(\*|[A-Za-z]+(?:\|[A-Za-z]+)*)\s+(.+)$")


def _glob_to_regex(glob: str) -> str:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("/**", i) and (i + 3 == len(glob) or glob[i + 3] == "/"):
            out.append("(?:/.*)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class Route:
    pattern: str
    methods: frozenset[str] | None
    regex: re.Pattern[str]

    def matches(self, method: str, path: str) -> bool:
        if self.methods is not None and method.upper() not in self.methods:
            return False
        return self.regex.fullmatch(path) is not None


@lru_cache(maxsize=512)
def compile_route(pattern: str) -> Route:
    text = pattern.strip()
    if not text:
        raise ValueError("empty route pattern")
    match = _METHODS.match(text)
    methods: frozenset[str] | None = None
    path = text
    if match:
        method_part, path = match.groups()
        if method_part != "*":
            methods = frozenset(m.upper() for m in method_part.split("|"))
    if not path.startswith("/") and path not in ("*", "**"):
        raise ValueError(f"route path must start with '/': {pattern!r}")
    if path in ("*", "**"):
        path = "/**"
    return Route(pattern, methods, re.compile(_glob_to_regex(path)))


def route_matches(pattern: str, method: str, path: str) -> bool:
    return compile_route(pattern).matches(method, path)
