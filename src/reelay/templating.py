"""Response templates for replay.

Exchanges marked ``"templated": true`` have ``{{ expression }}`` placeholders
in their response body and headers filled in per request. Expressions are
resolved against a fixed whitelist, never evaluated as code.

=============================  =========================================
Expression                     Value
=============================  =========================================
``request.method``             ``GET``
``request.path``               ``/users/42``
``request.path.N``             path segment N, 0-based (``users``)
``request.query.NAME``         query parameter
``request.header.NAME``        request header (case-insensitive)
``request.json.a.b.0``         field from a JSON request body
``uuid``                       random UUID4
``now`` / ``now_unix``         current time (ISO 8601 / epoch seconds)
``seq``                        per-server counter starting at 1
``random_int(A,B)``            random integer in [A, B]
=============================  =========================================

A JSON value that is exactly one placeholder (``"{{request.json.count}}"``)
keeps the original type of what it resolves to.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from reelay.bodies import Body
from reelay.models import Headers, Request, Response, header_get

_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_RANDOM_INT = re.compile(r"^random_int\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)$")
_MISSING = object()


class TemplateContext:
    def __init__(self, request: Request, counter: itertools.count[int], rng: random.Random | None = None) -> None:
        self.request = request
        self.counter = counter
        self.rng = rng or random.Random()

    def resolve(self, expr: str) -> Any:
        expr = expr.strip()
        if expr == "uuid":
            return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        if expr == "now":
            return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        if expr == "now_unix":
            return int(datetime.now(UTC).timestamp())
        if expr == "seq":
            return next(self.counter)
        if m := _RANDOM_INT.match(expr):
            lo, hi = int(m.group(1)), int(m.group(2))
            return self.rng.randint(min(lo, hi), max(lo, hi))
        parts = expr.split(".")
        if parts[0] != "request" or len(parts) < 2:
            return _MISSING
        req = self.request
        field = parts[1]
        if field == "method" and len(parts) == 2:
            return req.method
        if field == "path":
            if len(parts) == 2:
                return req.path
            segments = [s for s in req.path.split("/") if s]
            try:
                return segments[int(parts[2])]
            except (ValueError, IndexError):
                return _MISSING
        if field == "query" and len(parts) == 3:
            value = req.query_value(parts[2])
            return _MISSING if value is None else value
        if field == "header" and len(parts) == 3:
            value = header_get(req.headers, parts[2])
            return _MISSING if value is None else value
        if field == "json" and req.body.encoding == "json":
            node: Any = req.body.content
            for key in parts[2:]:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                elif isinstance(node, list) and key.lstrip("-").isdigit() and -len(node) <= int(key) < len(node):
                    node = node[int(key)]
                else:
                    return _MISSING
            return node
        return _MISSING

    def render_text(self, text: str) -> str:
        def sub(match: re.Match[str]) -> str:
            value = self.resolve(match.group(1))
            if value is _MISSING:
                return ""
            return value if isinstance(value, str) else _to_text(value)

        return _PLACEHOLDER.sub(sub, text)

    def render_json(self, node: Any) -> Any:
        if isinstance(node, str):
            whole = _PLACEHOLDER.fullmatch(node.strip())
            if whole:
                value = self.resolve(whole.group(1))
                return None if value is _MISSING else value
            return self.render_text(node)
        if isinstance(node, dict):
            return {self.render_text(k): self.render_json(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self.render_json(v) for v in node]
        return node


def _to_text(value: Any) -> str:
    if isinstance(value, bool) or value is None or isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def render_response(response: Response, ctx: TemplateContext) -> Response:
    headers: Headers = [(k, ctx.render_text(v)) for k, v in response.headers]
    body = response.body
    if body.encoding == "json":
        body = Body("json", ctx.render_json(body.content))
    elif body.encoding == "text":
        body = Body("text", ctx.render_text(str(body.content)))
    return Response(response.status, headers, body, response.reason)
