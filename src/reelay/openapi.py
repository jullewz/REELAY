"""Infer an OpenAPI 3.1 document from recorded traffic.

Steps:

1. **Path templating.** ``/users/42`` and ``/users/7`` become
   ``/users/{userId}``. A segment is a parameter when it looks like an
   identifier (number, UUID, long hex or token), or when at least three
   different literals appear at the same position under the same prefix
   with the same remaining structure.
2. **Grouping** exchanges by method and templated path.
3. **Schemas** for request bodies and for each response status, inferred
   from every sample (see :mod:`reelay.schema`), plus query parameters with
   types and ``required`` derived from how often they appear.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from reelay import __version__, schema
from reelay.bodies import media_type
from reelay.models import Exchange
from reelay.util import singular

_ID_PATTERNS = (
    re.compile(r"^\d+$"),
    re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    re.compile(r"^[0-9a-fA-F]{12,}$"),
    re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$"),  # ULID
)
VARIANCE_THRESHOLD = 3


def looks_like_id(segment: str) -> bool:
    return any(p.match(segment) for p in _ID_PATTERNS)


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _param_name(previous: str | None, used: set[str]) -> str:
    base = "id"
    if previous and not previous.startswith("{"):
        word = re.sub(r"[^A-Za-z0-9]+", " ", singular(previous)).title().replace(" ", "")
        if word:
            base = word[0].lower() + word[1:] + "Id"
    name, n = base, 2
    while name in used:
        name, n = f"{base}{n}", n + 1
    used.add(name)
    return name


def template_paths(paths: Iterable[str]) -> dict[str, str]:
    """Map each concrete path to its templated form."""
    unique = sorted(set(paths))
    split = {p: _segments(p) for p in unique}
    # pass 1: identifier-looking segments
    marked = {p: ["\0" if looks_like_id(s) else s for s in segs] for p, segs in split.items()}
    # pass 2: positions with many distinct literals under the same prefix and same shape
    groups: dict[tuple[int, tuple[str, ...], tuple[str, ...]], set[str]] = defaultdict(set)
    for segs in marked.values():
        for i, seg in enumerate(segs):
            if seg != "\0":
                groups[(i, tuple(segs[:i]), tuple(segs[i + 1 :]))].add(seg)
    variable = {key for key, values in groups.items() if len(values) >= VARIANCE_THRESHOLD}
    result: dict[str, str] = {}
    for path, segs in marked.items():
        out: list[str] = []
        used: set[str] = set()
        for i, seg in enumerate(segs):
            if seg == "\0" or (i, tuple(segs[:i]), tuple(segs[i + 1 :])) in variable:
                out.append("{" + _param_name(out[-1] if out else None, used) + "}")
            else:
                out.append(seg)
        result[path] = "/" + "/".join(out) if out else "/"
    return result


def _query_schema(values: list[str]) -> dict[str, Any]:
    if values and all(re.fullmatch(r"-?\d+", v) for v in values):
        return {"type": "integer"}
    if values and all(re.fullmatch(r"-?\d+(\.\d+)?", v) for v in values):
        return {"type": "number"}
    if values and all(v.lower() in ("true", "false") for v in values):
        return {"type": "boolean"}
    return {"type": "string"}


def _operation_id(method: str, path: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", path.replace("{", "by_").replace("}", "")) if w]
    return method.lower() + "".join(w[:1].upper() + w[1:] for w in words)


def build(exchanges: Iterable[Exchange], *, title: str = "Inferred API", server: str | None = None,
          version: str = "1.0.0", examples: bool = True) -> dict[str, Any]:  # fmt: skip
    exs = list(exchanges)
    mapping = template_paths(e.request.path for e in exs)
    ops: dict[tuple[str, str], list[Exchange]] = defaultdict(list)
    for ex in exs:
        ops[(mapping[ex.request.path], ex.request.method.lower())].append(ex)

    paths: dict[str, dict[str, Any]] = {}
    for (path, method), group in sorted(ops.items()):
        op: dict[str, Any] = {"operationId": _operation_id(method, path),
                              "summary": f"{method.upper()} {path}",
                              "x-reelay-samples": len(group)}  # fmt: skip
        params: list[dict[str, Any]] = []
        for name in re.findall(r"\{([^}]+)\}", path):
            idx = _segments(path).index("{" + name + "}")
            values = [_segments(e.request.path)[idx] for e in group]
            params.append({"name": name, "in": "path", "required": True, "schema": _query_schema(values),
                           **({"example": values[0]} if examples else {})})  # fmt: skip
        query_values: dict[str, list[str]] = defaultdict(list)
        presence: dict[str, int] = defaultdict(int)
        for ex in group:
            seen = set()
            for k, v in ex.request.query:
                query_values[k].append(v)
                seen.add(k)
            for k in seen:
                presence[k] += 1
        for name in sorted(query_values):
            params.append({"name": name, "in": "query", "required": presence[name] == len(group),
                           "schema": _query_schema(query_values[name])})  # fmt: skip
        if params:
            op["parameters"] = params

        bodies = [e.request for e in group if e.request.body.encoding != "empty"]
        if bodies:
            ctype = media_type(bodies[0].content_type) or "application/octet-stream"
            json_bodies = [r.body.content for r in bodies if r.body.encoding == "json"]
            content_schema = schema.infer_many(json_bodies) if json_bodies else {"type": "string"}
            media: dict[str, Any] = {"schema": content_schema}
            if examples and json_bodies:
                media["example"] = json_bodies[0]
            op["requestBody"] = {"required": len(bodies) == len(group), "content": {ctype: media}}

        responses: dict[str, Any] = {}
        by_status: dict[int, list[Exchange]] = defaultdict(list)
        for ex in group:
            by_status[ex.response.status].append(ex)
        for status, items in sorted(by_status.items()):
            entry: dict[str, Any] = {"description": items[0].response.reason or f"HTTP {status}"}
            with_body = [e.response for e in items if e.response.body.encoding != "empty"]
            if with_body:
                ctype = media_type(with_body[0].content_type) or "application/octet-stream"
                json_docs = [r.body.content for r in with_body if r.body.encoding == "json"]
                media = {"schema": schema.infer_many(json_docs) if json_docs else {"type": "string"}}
                if examples and json_docs:
                    media["example"] = json_docs[0]
                entry["content"] = {ctype: media}
            responses[str(status)] = entry
        op["responses"] = responses
        paths.setdefault(path, {})[method] = op

    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version,
                 "description": f"Inferred by Reelay {__version__} from {len(exs)} recorded exchanges."},
        "paths": paths,
    }  # fmt: skip
    if server:
        doc["servers"] = [{"url": server}]
    return doc
