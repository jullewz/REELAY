"""The Reelay HTTP server: recording proxy, replay mock and live dashboard.

Request handling is split in two:

* :class:`Engine` decides what to answer. It is plain Python with no
  sockets, so it is easy to test.
* :class:`_Handler` speaks HTTP/1.1 (keep-alive, chunked request bodies,
  HEAD, bandwidth throttling, server-sent events) and delegates to it.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import queue
import random
import socket
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from reelay import __version__
from reelay.bodies import Body
from reelay.chaos import ChaosDecision
from reelay.config import MODES, Settings
from reelay.errors import ConfigError, UpstreamError
from reelay.events import EventBus, TrafficEvent, make_event
from reelay.matching import Matcher
from reelay.models import Exchange, Headers, Request, Response, header_get
from reelay.redact import Redactor
from reelay.tape import Tape
from reelay.templating import TemplateContext, render_response
from reelay.upstream import HOP_BY_HOP, Target, UpstreamClient

log = logging.getLogger(__name__)

_IDLE: Any = object()  # sentinel: no event arrived within the poll interval
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")


@dataclass(slots=True)
class Outcome:
    response: Response
    event: TrafficEvent
    chaos: ChaosDecision
    drop: bool = False


def _json_response(status: int, payload: Any, extra: Headers | None = None) -> Response:
    headers: Headers = [("Content-Type", "application/json; charset=utf-8")]
    headers += extra or []
    return Response(status, headers, Body("json", payload), HTTPStatus(status).phrase)


class Engine:
    """Decides how to answer each request according to the current mode."""

    def __init__(self, settings: Settings, tape: Tape | None) -> None:
        self.settings = settings
        srv = settings.server
        if srv.mode in ("record", "hybrid", "passthrough") and not srv.target:
            raise ConfigError(f"mode {srv.mode!r} needs an upstream target", hint="pass --target https://...")
        if srv.mode in ("record", "replay", "hybrid") and tape is None:
            raise ConfigError(f"mode {srv.mode!r} needs a tape", hint="pass --tape PATH")
        if srv.on_miss in ("proxy", "record") and srv.mode == "replay" and not srv.target:
            raise ConfigError(f"on_miss={srv.on_miss!r} needs an upstream target")
        self.tape = tape
        self.redactor = Redactor(settings.redact)
        self.matcher = Matcher(tape.exchanges if tape else (), settings.match, self.redactor)
        self.upstream = (UpstreamClient(Target.parse(srv.target), timeout=srv.timeout, verify_tls=srv.verify_tls,
                                        max_body=srv.max_body_size) if srv.target else None)  # fmt: skip
        self.bus = EventBus()
        self._mode = srv.mode
        self._mode_lock = threading.Lock()
        self._counter = itertools.count(1)
        self._rng = random.Random(settings.chaos.seed)

    # ------------------------------------------------------------- mode
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode in ("record", "hybrid", "passthrough") and self.upstream is None:
            raise ValueError(f"mode {mode!r} needs an upstream target")
        if mode in ("record", "replay", "hybrid") and self.tape is None:
            raise ValueError(f"mode {mode!r} needs a tape")
        with self._mode_lock:
            self._mode = mode
        log.info("mode switched to %s", mode)

    # ---------------------------------------------------------- handling
    def handle(self, req: Request, client_ip: str | None = None) -> Outcome:
        started = time.perf_counter()
        decision = self.settings.chaos.decide(req.method, req.path)
        if decision.delay:
            time.sleep(decision.delay)
        if decision.drop:
            resp = _json_response(502, {"error": "connection dropped by reelay chaos"})
            event = make_event("chaos", req, resp, self._ms(started), chaos=decision.describe())
            return Outcome(resp, self._sanitize(event), decision, drop=True)
        if decision.error_status:
            resp = _json_response(decision.error_status, {"error": "injected by reelay chaos",
                                                          "rule": decision.rule},
                                  [("X-Reelay", "chaos")])  # fmt: skip
            event = make_event("chaos", req, resp, self._ms(started), chaos=decision.describe())
            return Outcome(resp, self._sanitize(event), decision)

        mode = self.mode
        try:
            if mode == "passthrough":
                resp, event = self._proxy(req, client_ip, save=False, started=started)
            elif mode == "record":
                resp, event = self._proxy(req, client_ip, save=True, started=started)
            else:
                resp, event = self._replay(req, client_ip, started, hybrid=mode == "hybrid")
        except UpstreamError as exc:
            log.warning("%s", exc.message)
            resp = _json_response(502, {"error": "upstream request failed", "detail": exc.message},
                                  [("X-Reelay", "error")])  # fmt: skip
            event = make_event("error", req, resp, self._ms(started), note=exc.message)
        if decision.active:
            event.chaos = decision.describe()
        self._sanitize(event)
        if self.settings.server.cors:
            resp = Response(resp.status, [*resp.headers, *_cors_headers(req)], resp.body, resp.reason)
        return Outcome(resp, event, decision)

    def _proxy(
        self, req: Request, client_ip: str | None, *, save: bool, started: float
    ) -> tuple[Response, TrafficEvent]:
        assert self.upstream is not None
        resp, upstream_ms = self.upstream.send(req, client_ip)
        recorded_req = Request(req.method, req.path, req.query, _recordable_headers(req.headers), req.body)
        exchange = Exchange(recorded_req, resp, duration_ms=upstream_ms, upstream=self.upstream.target.url)
        if save and self.tape is not None:
            clean = self.redactor.exchange(exchange)
            self.tape.append(clean)
            self.matcher.add(clean)
            exchange = clean
        source = "recorded" if save else "proxied"
        tagged = Response(resp.status, [*resp.headers, ("X-Reelay", source)], resp.body, resp.reason)
        return tagged, make_event(source, req, tagged, self._ms(started), exchange=exchange)

    def _replay(
        self, req: Request, client_ip: str | None, started: float, *, hybrid: bool
    ) -> tuple[Response, TrafficEvent]:
        result = self.matcher.find(req)
        if result.exchange is not None:
            ex = result.exchange
            resp = ex.response
            if ex.templated:
                resp = render_response(resp, TemplateContext(req, self._counter, self._rng))
            headers = [*resp.headers, ("X-Reelay", "replayed"), ("X-Reelay-Match", result.kind),
                       ("X-Reelay-Exchange", ex.id)]  # fmt: skip
            out = Response(resp.status, headers, resp.body, resp.reason)
            return out, make_event("replayed", req, out, self._ms(started), match=result.kind, exchange=ex)

        on_miss = "record" if hybrid else self.settings.server.on_miss
        if on_miss in ("proxy", "record") and self.upstream is not None:
            return self._proxy(req, client_ip, save=on_miss == "record", started=started)

        if req.method == "OPTIONS" and self.settings.server.cors:
            resp = Response(204, [("X-Reelay", "cors")], Body.empty(), "No Content")
            return resp, make_event("replayed", req, resp, self._ms(started), match="cors")

        candidates = [
            {"exchange": c.exchange.id, "request": c.exchange.label, "status": c.exchange.response.status,
             "differences": list(c.reasons)}
            for c in result.candidates
        ]  # fmt: skip
        payload = {
            "error": "no recorded exchange matches this request",
            "request": f"{req.method} {req.target}",
            "match_on": list(self.settings.match.on),
            "closest": candidates,
            "hint": "record it (mode=hybrid or on_miss=record), or relax [match] settings",
        }
        resp = _json_response(501, payload, [("X-Reelay", "miss")])
        first = result.candidates[0] if result.candidates else None
        note = first.reasons[0] if first and first.reasons else None
        return resp, make_event("miss", req, resp, self._ms(started), match="miss", note=note)

    def _sanitize(self, event: TrafficEvent) -> TrafficEvent:
        """Live views (dashboard, terminal log) never show secrets, whatever the mode."""
        if event.exchange is not None:
            event.exchange = self.redactor.exchange(event.exchange)
            event.target = event.exchange.request.target
        return event

    @staticmethod
    def _ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000


def _recordable_headers(headers: Headers) -> Headers:
    """Request headers worth keeping on a tape (no hop-by-hop or transport noise)."""
    drop = {*HOP_BY_HOP, "host", "content-length", "accept-encoding"}
    return [(k, v) for k, v in headers if k.lower() not in drop]


def _cors_headers(req: Request) -> Headers:
    origin = header_get(req.headers, "origin") or "*"
    return [("Access-Control-Allow-Origin", origin), ("Access-Control-Allow-Credentials", "true"),
            ("Access-Control-Allow-Headers", header_get(req.headers, "access-control-request-headers") or "*"),
            ("Access-Control-Allow-Methods", ", ".join(METHODS)), ("Vary", "Origin")]  # fmt: skip


# ------------------------------------------------------------------- HTTP layer
class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], engine: Engine, stopping: threading.Event) -> None:
        self.engine = engine
        self.stopping = stopping
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"reelay/{__version__}"
    server: _HTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), format % args)

    # ----------------------------------------------------------- reading
    def _read_body(self) -> bytes | None:
        limit = self.server.engine.settings.server.max_body_size
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65537)
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                total += size
                if total > limit:
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline(3)
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _dispatch(self) -> None:
        engine = self.server.engine
        prefix = engine.settings.server.control_prefix
        path = urlsplit(self.path).path
        if path == prefix or path.startswith(prefix + "/"):
            self._control(path[len(prefix) :] or "", prefix)
            return
        try:
            raw = self._read_body()
        except (ValueError, OSError):
            self._send(_json_response(400, {"error": "malformed request body"}), close=True)
            return
        if raw is None:
            self._send(_json_response(413, {"error": "request body exceeds max_body_size"}), close=True)
            return
        headers: Headers = list(self.headers.items())
        req = Request.from_target(self.command, self.path, headers, raw)
        outcome = engine.handle(req, self.client_address[0])
        engine.bus.publish(outcome.event)
        if outcome.drop:
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            return
        self._send(outcome.response, bandwidth=outcome.chaos.bandwidth)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_") and name[3:] in METHODS:
            return self._dispatch
        raise AttributeError(name)

    # ----------------------------------------------------------- writing
    def _send(self, resp: Response, *, bandwidth: int | None = None, close: bool = False) -> None:
        body = resp.body.to_bytes(resp.content_type)
        try:
            self.send_response(resp.status, resp.reason or None)
            for name, value in resp.headers:
                if name.lower() not in ("content-length", "transfer-encoding", "connection", "date", "server"):
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200:
                return
            if bandwidth:
                chunk = max(256, bandwidth // 10)
                for i in range(0, len(body), chunk):
                    self.wfile.write(body[i : i + chunk])
                    self.wfile.flush()
                    time.sleep(len(body[i : i + chunk]) / bandwidth)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ----------------------------------------------------------- control API
    def _control(self, sub: str, prefix: str) -> None:
        engine = self.server.engine
        method = self.command
        if method in ("POST", "PUT", "DELETE") and self.headers.get("X-Reelay-Control") != "1":
            # A custom header cannot be sent cross-origin without a CORS preflight we never approve,
            # so this blocks drive-by requests from web pages to the control API.
            self._read_body()
            self._send(_json_response(403, {"error": "control requests need the X-Reelay-Control: 1 header"}))
            return
        if sub == "":
            self._send(Response(308, [("Location", prefix + "/")], Body.empty(), "Permanent Redirect"))
            return
        if sub in ("/", "/index.html", "/app.js", "/style.css"):
            if not engine.settings.server.dashboard:
                self._send(_json_response(404, {"error": "dashboard disabled"}))
                return
            name = "index.html" if sub in ("/", "/index.html") else sub[1:]
            ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
            data = resources.files("reelay.dashboard").joinpath(name).read_text(encoding="utf-8")
            data = data.replace("{{PREFIX}}", prefix).replace("{{VERSION}}", __version__)
            self._send(Response(200, [("Content-Type", f"{ctype}; charset=utf-8"), ("Cache-Control", "no-store"),
                                      ("X-Content-Type-Options", "nosniff")],
                                Body("text", data)))  # fmt: skip
            return
        if sub == "/api/events":
            self._sse()
            return
        if sub == "/api/health":
            self._send(_json_response(200, {"status": "ok", "version": __version__, "mode": engine.mode}))
        elif sub == "/api/traffic" and method == "GET":
            self._send(_json_response(200, [e.summary() for e in engine.bus.recent(300)]))
        elif sub.startswith("/api/traffic/") and method == "GET":
            event = engine.bus.get(sub.rsplit("/", 1)[1])
            if event is None:
                self._send(_json_response(404, {"error": "unknown event"}))
            else:
                self._send(_json_response(200, event.detail()))
        elif sub == "/api/stats":
            self._send(_json_response(200, self._stats()))
        elif sub == "/api/mode" and method == "GET":
            self._send(_json_response(200, {"mode": engine.mode, "modes": list(MODES)}))
        elif sub == "/api/mode" and method == "POST":
            raw = self._read_body() or b"{}"
            try:
                engine.set_mode(str(json.loads(raw).get("mode")))
            except (ValueError, AttributeError) as exc:
                self._send(_json_response(400, {"error": str(exc)}))
            else:
                self._send(_json_response(200, {"mode": engine.mode}))
        elif sub == "/api/reset" and method == "POST":
            self._read_body()
            engine.matcher.reset()
            self._send(_json_response(200, {"reset": True}))
        elif sub == "/api/clear" and method == "POST":
            self._read_body()
            engine.bus.clear()
            self._send(_json_response(200, {"cleared": True}))
        else:
            self._send(_json_response(404, {"error": f"unknown control endpoint {sub}"}))

    def _stats(self) -> dict[str, Any]:
        engine = self.server.engine
        srv = engine.settings.server
        tape = engine.tape
        return {
            **engine.bus.stats(),
            "mode": engine.mode,
            "version": __version__,
            "tape": {"path": str(tape.path) if tape and tape.path else None, "exchanges": len(tape) if tape else 0},
            "target": srv.target,
            "chaos": {"enabled": engine.settings.chaos.enabled, "rules": len(engine.settings.chaos.rules)},
        }

    def _sse(self) -> None:
        engine = self.server.engine
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
        except OSError:
            return
        sub = engine.bus.subscribe()
        last = time.monotonic()
        try:
            while not self.server.stopping.is_set():
                try:
                    event = sub.get(timeout=0.5)
                except queue.Empty:
                    event = _IDLE
                if event is None:
                    break
                if event is not _IDLE:
                    payload = json.dumps(event.summary(), ensure_ascii=False)
                    self.wfile.write(f"event: traffic\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    last = time.monotonic()
                elif time.monotonic() - last > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last = time.monotonic()
        except OSError:
            pass
        finally:
            engine.bus.unsubscribe(sub)


class ReelayServer:
    """Run an :class:`Engine` behind a threaded HTTP server.

    Usable as a context manager, which is how the pytest plugin and the
    test-suite run it::

        with ReelayServer(settings, tape) as server:
            requests.get(server.url + "/users")
    """

    def __init__(self, settings: Settings, tape: Tape | None) -> None:
        self.engine = Engine(settings, tape)
        self._stopping = threading.Event()
        srv = settings.server
        try:
            self._httpd = _HTTPServer((srv.host, srv.port), self.engine, self._stopping)
        except OSError as exc:
            raise ConfigError(f"cannot listen on {srv.host}:{srv.port}: {exc.strerror or exc}",
                              hint="pick another --port, or 0 for any free port") from None  # fmt: skip
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else ("[::1]" if host == "::" else host)  # noqa: S104
        return f"http://{shown}:{port}"

    def start(self) -> ReelayServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="reelay-server", daemon=True)  # fmt: skip
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.2)

    def close(self) -> None:
        """Release the socket after :meth:`serve_forever` returned (for example on Ctrl+C)."""
        self._stopping.set()
        self.engine.bus.close()
        self._httpd.server_close()

    def stop(self) -> None:
        self._stopping.set()
        self.engine.bus.close()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> ReelayServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
