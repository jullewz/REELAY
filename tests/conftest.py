from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request

import pytest

from reelay.bodies import Body
from reelay.models import Exchange, Request, Response


def pytest_configure(config: pytest.Config) -> None:
    """Load the plugin from the source tree when Reelay is not installed (installed copies register it themselves)."""
    import reelay.pytest_plugin as plugin

    manager = config.pluginmanager
    if not manager.has_plugin("reelay") and not manager.is_registered(plugin):
        manager.register(plugin, "reelay")


class FakeAPI(BaseHTTPRequestHandler):
    """A small but realistic upstream used by the integration tests."""

    protocol_version = "HTTP/1.1"
    calls: list[str] = []
    counter = {"job": 0}

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None, *, gz: bool = False) -> None:
        raw = json.dumps(payload).encode()
        if gz:
            raw = gzip.compress(raw)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeAPI.calls.append(f"{self.command} {self.path}")
        path = self.path.split("?")[0]
        if path.startswith("/users/") and self.command == "GET":
            uid = path.rsplit("/", 1)[1]
            self._json(
                200,
                {
                    "id": int(uid) if uid.isdigit() else uid,
                    "name": f"user {uid}",
                    "email": f"u{uid}@example.com",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            )
        elif path == "/users" and self.command == "POST":
            data = json.loads(body or b"{}")
            self._json(201, {"id": 99, **data, "token": "tok_live_123"}, {"Location": "/users/99"})
        elif path == "/gzip":
            self._json(200, {"compressed": True}, gz=True)
        elif path == "/job":
            FakeAPI.counter["job"] += 1
            self._json(200, {"state": "done" if FakeAPI.counter["job"] >= 3 else "pending"})
        elif path == "/text":
            raw = b"hello plain world"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/headers":
            self._json(
                200,
                {
                    "auth": self.headers.get("Authorization"),
                    "xff": self.headers.get("X-Forwarded-For"),
                    "host": self.headers.get("Host"),
                },
            )
        else:
            self._json(404, {"error": "not found"})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle


@pytest.fixture
def upstream() -> Iterator[str]:
    FakeAPI.calls = []
    FakeAPI.counter = {"job": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def http(
    method: str, url: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        hdrs.setdefault("Content-Type", "application/json")
    req = request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with request.urlopen(req, timeout=10) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def jget(url: str, **kw: Any) -> tuple[int, dict[str, str], Any]:
    status, headers, raw = http("GET", url, **kw)
    return status, headers, json.loads(raw) if raw else None


def ex(method: str = "GET", path: str = "/users/1", *, query: list[tuple[str, str]] | None = None,
       body: Any = None, status: int = 200, resp: Any = None, headers: list[tuple[str, str]] | None = None,
       **kw: Any) -> Exchange:  # fmt: skip
    req_body = Body("json", body) if body is not None else Body.empty()
    req_headers = list(headers or []) + ([("Content-Type", "application/json")] if body is not None else [])
    response = Response(status, [("Content-Type", "application/json")],
                        Body("json", resp if resp is not None else {"ok": True}), "OK")  # fmt: skip
    return Exchange(Request(method, path, query or [], req_headers, req_body), response, **kw)


@pytest.fixture
def tape_path(tmp_path: Path) -> Path:
    return tmp_path / "tapes" / "api.tape"
