"""HAR 1.2 import and export (browser devtools, Charles, mitmproxy, k6...)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from reelay import __version__
from reelay.bodies import Body
from reelay.errors import TapeError
from reelay.models import Exchange, Request, Response, header_get
from reelay.util import new_id


def _iso(ts: str) -> str:
    return ts.replace("Z", "+00:00") if ts.endswith("Z") else ts


def from_har(data: dict[str, Any]) -> tuple[list[Exchange], str | None]:
    """Convert a HAR document into exchanges. Returns ``(exchanges, upstream)``."""
    try:
        entries = data["log"]["entries"]
    except (KeyError, TypeError):
        raise TapeError("not a HAR file: missing log.entries") from None
    exchanges: list[Exchange] = []
    upstream: str | None = None
    for i, entry in enumerate(entries):
        try:
            req, resp = entry["request"], entry["response"]
            url = urlsplit(req["url"])
            if upstream is None and url.scheme and url.netloc:
                upstream = f"{url.scheme}://{url.netloc}"
            headers = [(h["name"], h["value"]) for h in req.get("headers", []) if not h["name"].startswith(":")]
            post = req.get("postData") or {}
            raw = post.get("text", "").encode("utf-8")
            if post.get("encoding") == "base64":
                raw = base64.b64decode(post.get("text", ""))
            target = url.path + (f"?{url.query}" if url.query else "")
            request = Request.from_target(req["method"], target or "/", headers, raw)
            if post.get("mimeType") and not header_get(headers, "content-type"):
                request = Request(request.method, request.path, request.query, request.headers,
                                  Body.from_bytes(raw, post["mimeType"]))  # fmt: skip
            content = resp.get("content") or {}
            rheaders = [(h["name"], h["value"]) for h in resp.get("headers", []) if not h["name"].startswith(":")]
            rheaders = [(k, v) for k, v in rheaders if k.lower() not in ("content-encoding", "content-length")]
            text = content.get("text") or ""
            body_bytes = base64.b64decode(text) if content.get("encoding") == "base64" else text.encode("utf-8")
            ctype = header_get(rheaders, "content-type") or content.get("mimeType")
            response = Response(int(resp["status"]), rheaders, Body.from_bytes(body_bytes, ctype),
                                resp.get("statusText", ""))  # fmt: skip
            exchanges.append(Exchange(request, response, id=new_id(),
                                      recorded_at=entry.get("startedDateTime") or datetime.now(UTC).isoformat(),
                                      duration_ms=float(entry.get("time") or 0), upstream=upstream))  # fmt: skip
        except (KeyError, TypeError, ValueError) as exc:
            raise TapeError(f"HAR entry #{i + 1} is malformed: {exc}") from None
    return exchanges, upstream


def _har_body(body: Body, ctype: str | None) -> dict[str, Any]:
    data = body.to_bytes(ctype)
    out: dict[str, Any] = {"size": len(data), "mimeType": ctype or ""}
    if body.encoding == "base64":
        out.update(text=body.content, encoding="base64")
    elif data:
        out["text"] = data.decode("utf-8", "replace")
    return out


def to_har(exchanges: list[Exchange], upstream: str | None) -> dict[str, Any]:
    base = (upstream or "http://localhost").rstrip("/")
    entries = []
    for ex in exchanges:
        req, resp = ex.request, ex.response
        body = req.body.to_bytes(req.content_type)
        entry_req: dict[str, Any] = {
            "method": req.method, "url": base + req.target, "httpVersion": "HTTP/1.1",
            "headers": [{"name": k, "value": v} for k, v in req.headers],
            "queryString": [{"name": k, "value": v} for k, v in req.query],
            "cookies": [], "headersSize": -1, "bodySize": len(body),
        }  # fmt: skip
        if body:
            post = _har_body(req.body, req.content_type)
            entry_req["postData"] = {"mimeType": post["mimeType"], "text": post.get("text", "")}
        content = _har_body(resp.body, resp.content_type)
        entries.append({
            "startedDateTime": _iso(ex.recorded_at),
            "time": ex.duration_ms,
            "request": entry_req,
            "response": {"status": resp.status, "statusText": resp.reason, "httpVersion": "HTTP/1.1",
                         "headers": [{"name": k, "value": v} for k, v in resp.headers], "cookies": [],
                         "content": content, "redirectURL": header_get(resp.headers, "location") or "",
                         "headersSize": -1, "bodySize": content["size"]},
            "cache": {},
            "timings": {"send": 0, "wait": ex.duration_ms, "receive": 0},
            "comment": f"reelay exchange {ex.id}",
        })  # fmt: skip
    return {"log": {"version": "1.2", "creator": {"name": "reelay", "version": __version__}, "entries": entries}}
