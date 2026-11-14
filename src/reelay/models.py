"""Core data model: requests, responses and recorded exchanges."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from reelay.bodies import Body
from reelay.errors import TapeError
from reelay.util import new_id, utc_now

Headers = list[tuple[str, str]]


def header_get(headers: Headers, name: str, default: str | None = None) -> str | None:
    lname = name.lower()
    for key, value in headers:
        if key.lower() == lname:
            return value
    return default


def header_all(headers: Headers, name: str) -> list[str]:
    lname = name.lower()
    return [v for k, v in headers if k.lower() == lname]


def header_without(headers: Headers, *names: str) -> Headers:
    drop = {n.lower() for n in names}
    return [(k, v) for k, v in headers if k.lower() not in drop]


def _pairs(data: Any, what: str) -> list[tuple[str, str]]:
    if not isinstance(data, list):
        raise TapeError(f"{what} must be a list of [name, value] pairs")
    out: list[tuple[str, str]] = []
    for item in data:
        if not (isinstance(item, list | tuple) and len(item) == 2):
            raise TapeError(f"malformed {what} entry: {item!r:.60}")
        out.append((str(item[0]), str(item[1])))
    return out


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: list[tuple[str, str]] = field(default_factory=list)
    headers: Headers = field(default_factory=list)
    body: Body = field(default_factory=Body.empty)

    @classmethod
    def from_target(cls, method: str, target: str, headers: Headers, raw_body: bytes) -> Request:
        """Build a request from the raw request line target (``/a/b?x=1``)."""
        parts = urlsplit(target)
        path = parts.path or "/"
        query = parse_qsl(parts.query, keep_blank_values=True)
        return cls(method.upper(), path, query, headers, Body.from_bytes(raw_body, header_get(headers, "content-type")))

    @property
    def target(self) -> str:
        return self.path + (f"?{urlencode(self.query, safe='[]/:,')}" if self.query else "")

    @property
    def content_type(self) -> str | None:
        return header_get(self.headers, "content-type")

    def query_value(self, name: str) -> str | None:
        for key, value in self.query:
            if key == name:
                return value
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "query": [list(p) for p in self.query],
            "headers": [list(h) for h in self.headers],
            "body": self.body.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Request:
        try:
            return cls(
                method=str(data["method"]).upper(),
                path=str(data["path"]),
                query=_pairs(data.get("query", []), "query"),
                headers=_pairs(data.get("headers", []), "headers"),
                body=Body.from_dict(data.get("body")),
            )
        except KeyError as exc:
            raise TapeError(f"request is missing {exc}") from None


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Headers = field(default_factory=list)
    body: Body = field(default_factory=Body.empty)
    reason: str = ""

    @property
    def content_type(self) -> str | None:
        return header_get(self.headers, "content-type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "headers": [list(h) for h in self.headers],
            "body": self.body.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Response:
        try:
            status = int(data["status"])
        except (KeyError, TypeError, ValueError):
            raise TapeError("response needs an integer 'status'") from None
        if not 100 <= status <= 599:
            raise TapeError(f"invalid response status {status}")
        return cls(status, _pairs(data.get("headers", []), "headers"), Body.from_dict(data.get("body")),
                   str(data.get("reason", "")))  # fmt: skip


@dataclass(frozen=True, slots=True)
class Exchange:
    """One recorded request/response pair."""

    request: Request
    response: Response
    id: str = field(default_factory=new_id)
    recorded_at: str = field(default_factory=utc_now)
    duration_ms: float = 0.0
    upstream: str | None = None
    tags: tuple[str, ...] = ()
    templated: bool = False

    @property
    def label(self) -> str:
        return f"{self.request.method} {self.request.target}"

    def with_(self, **changes: Any) -> Exchange:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "recorded_at": self.recorded_at,
            "duration_ms": round(self.duration_ms, 3),
            "upstream": self.upstream,
            "request": self.request.to_dict(),
            "response": self.response.to_dict(),
        }
        if self.tags:
            data["tags"] = list(self.tags)
        if self.templated:
            data["templated"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Exchange:
        if not isinstance(data, dict):
            raise TapeError("exchange must be a JSON object")
        try:
            return cls(
                request=Request.from_dict(data["request"]),
                response=Response.from_dict(data["response"]),
                id=str(data.get("id") or new_id()),
                recorded_at=str(data.get("recorded_at") or utc_now()),
                duration_ms=float(data.get("duration_ms") or 0.0),
                upstream=data.get("upstream"),
                tags=tuple(str(t) for t in data.get("tags", [])),
                templated=bool(data.get("templated", False)),
            )
        except KeyError as exc:
            raise TapeError(f"exchange is missing {exc}") from None
