"""End-to-end server tests over real sockets against a local fake upstream."""

from __future__ import annotations

import http.client as http_client
import json
import socket
import threading
import time
from pathlib import Path
from urllib import request

import pytest

from reelay.chaos import ChaosEngine, ChaosRule, Latency
from reelay.config import Settings
from reelay.errors import ConfigError
from reelay.server import ReelayServer
from reelay.tape import Tape
from reelay.testing import replay

from .conftest import FakeAPI, ex, http, jget


def settings(mode: str, target: str | None = None, **server: object) -> Settings:
    s = Settings()
    s.server.port = 0
    s.server.mode = mode
    s.server.target = target
    for k, v in server.items():
        setattr(s.server, k, v)
    return s


def test_record_then_replay_offline(upstream: str, tape_path: Path) -> None:
    tape = Tape.open_or_create(tape_path, upstream)
    with ReelayServer(settings("record", upstream), tape) as srv:
        status, headers, body = jget(srv.url + "/users/7?api_key=k", headers={"Authorization": "Bearer s3cr3t-value"})
        assert status == 200 and body["name"] == "user 7" and headers["x-reelay"] == "recorded"
        status, headers, body = jget(srv.url + "/gzip")
        assert body == {"compressed": True} and "content-encoding" not in headers
        status, _, raw = http("POST", srv.url + "/users", {"name": "ada", "password": "pw"})
        assert status == 201 and json.loads(raw)["token"] == "tok_live_123"  # client gets the real response

    saved = tape_path.read_text()
    assert "s3cr3t" not in saved and '"pw"' not in saved and "tok_live_123" not in saved
    assert '"api_key", "[REDACTED]"' in saved
    calls_before = len(FakeAPI.calls)

    # replay works with the upstream unused (no target configured at all)
    with ReelayServer(settings("replay"), Tape.load(tape_path)) as srv:
        status, headers, body = jget(srv.url + "/users/7?api_key=other", headers={"Authorization": "Bearer x"})
        assert status == 200 and body["name"] == "user 7"
        assert headers["x-reelay"] == "replayed" and headers["x-reelay-match"] == "exact"
        status, _, raw = http("POST", srv.url + "/users", {"name": "ada", "password": "different"})
        assert status == 201
        status, headers, body = jget(srv.url + "/users/8")
        assert status == 501 and headers["x-reelay"] == "miss"
        assert body["closest"] and body["hint"]
    assert len(FakeAPI.calls) == calls_before


def test_hybrid_records_only_misses(upstream: str, tape_path: Path) -> None:
    with ReelayServer(settings("hybrid", upstream), Tape.open_or_create(tape_path, upstream)) as srv:
        assert jget(srv.url + "/users/1")[1]["x-reelay"] == "recorded"
        assert jget(srv.url + "/users/1")[1]["x-reelay"] == "replayed"
    assert FakeAPI.calls == ["GET /users/1"]
    assert len(Tape.load(tape_path)) == 1


def test_sequential_replay_of_polling(upstream: str, tape_path: Path) -> None:
    tape = Tape.open_or_create(tape_path, upstream)
    with ReelayServer(settings("record", upstream), tape) as srv:
        states = [jget(srv.url + "/job")[2]["state"] for _ in range(3)]
    assert states == ["pending", "pending", "done"]
    with ReelayServer(settings("replay"), Tape.load(tape_path)) as srv:
        replayed = [jget(srv.url + "/job")[2]["state"] for _ in range(5)]
        assert replayed == ["pending", "pending", "done", "done", "done"]
        assert http("POST", srv.url + "/__reelay/api/reset", b"", {"X-Reelay-Control": "1"})[0] == 200
        assert jget(srv.url + "/job")[2]["state"] == "pending"


def test_on_miss_proxy_and_record(upstream: str, tape_path: Path) -> None:
    Tape.open_or_create(tape_path, upstream)
    with ReelayServer(settings("replay", upstream, on_miss="proxy"), Tape.load(tape_path)) as srv:
        assert jget(srv.url + "/users/3")[1]["x-reelay"] == "proxied"
    assert len(Tape.load(tape_path)) == 0
    with ReelayServer(settings("replay", upstream, on_miss="record"), Tape.load(tape_path)) as srv:
        assert jget(srv.url + "/users/3")[1]["x-reelay"] == "recorded"
    assert len(Tape.load(tape_path)) == 1


def test_live_views_never_show_secrets(upstream: str) -> None:
    with ReelayServer(settings("passthrough", upstream), None) as srv:
        jget(srv.url + "/users/1?api_key=sk_live_abcdefghijklmnopqrst", headers={"Authorization": "Bearer abcdefgh"})
        event = srv.engine.bus.recent()[0]
        detail = json.dumps(event.detail())
    assert "sk_live" not in detail and "abcdefgh" not in detail and "[REDACTED]" in event.target


def test_passthrough_forwarding_headers(upstream: str) -> None:
    with ReelayServer(settings("passthrough", upstream), None) as srv:
        _, headers, body = jget(srv.url + "/headers", headers={"Authorization": "Bearer keep-me-upstream"})
    assert body["auth"] == "Bearer keep-me-upstream"
    assert body["xff"] == "127.0.0.1"
    assert body["host"] == upstream.removeprefix("http://")
    assert headers["x-reelay"] == "proxied"


def test_text_head_and_404_passthrough(upstream: str, tape_path: Path) -> None:
    with ReelayServer(settings("record", upstream), Tape.open_or_create(tape_path, upstream)) as srv:
        _, headers, raw = http("GET", srv.url + "/text")
        assert raw == b"hello plain world" and headers["content-type"].startswith("text/plain")
        assert http("GET", srv.url + "/nothing")[0] == 404
    with ReelayServer(settings("replay"), Tape.load(tape_path)) as srv:
        assert http("GET", srv.url + "/text")[2] == b"hello plain world"
        assert http("GET", srv.url + "/nothing")[0] == 404  # recorded 404s replay as 404s


def test_upstream_down_returns_502(tape_path: Path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with ReelayServer(
        settings("record", f"http://127.0.0.1:{port}", timeout=2.0), Tape.open_or_create(tape_path)
    ) as srv:
        status, headers, body = jget(srv.url + "/x")
    assert status == 502 and headers["x-reelay"] == "error" and "upstream" in body["error"]


def test_chaos_errors_latency_and_drops(tape_path: Path) -> None:
    tape = Tape.new(tape_path)
    tape.replace_all([ex(path="/slow"), ex(path="/flaky"), ex(path="/drop")])
    tape.save()
    s = settings("replay")
    s.chaos = ChaosEngine([ChaosRule("GET /slow", latency=Latency.parse("150ms")),
                           ChaosRule("GET /flaky", error_rate=1.0, error_status=429),
                           ChaosRule("GET /drop", drop_rate=1.0)], seed=1)  # fmt: skip
    with ReelayServer(s, Tape.load(tape_path)) as srv:
        started = time.perf_counter()
        assert jget(srv.url + "/slow")[0] == 200
        assert time.perf_counter() - started >= 0.14
        status, headers, _ = jget(srv.url + "/flaky")
        assert status == 429 and headers["x-reelay"] == "chaos"
        with pytest.raises((OSError, http_client.HTTPException)):
            request.urlopen(srv.url + "/drop", timeout=5).read()
        sources = srv.engine.bus.stats()["by_source"]
        assert sources["chaos"] == 2


def test_bandwidth_throttling(tape_path: Path) -> None:
    big = ex(path="/big", resp={"data": "x" * 20000})
    tape = Tape.new(tape_path)
    tape.replace_all([big])
    tape.save()
    s = settings("replay")
    s.chaos = ChaosEngine([ChaosRule("/big", bandwidth=40_000)])
    with ReelayServer(s, Tape.load(tape_path)) as srv:
        started = time.perf_counter()
        status, _, body = jget(srv.url + "/big")
    assert status == 200 and len(body["data"]) == 20000
    assert time.perf_counter() - started >= 0.4


def test_templated_replay(tape_path: Path) -> None:
    templ = ex("POST", "/orders", body={"sku": "A"}, status=201,
               resp={"id": "{{seq}}", "sku": "{{request.json.sku}}", "path": "{{request.path}}"},
               templated=True)  # fmt: skip
    from reelay.matching import MatchSettings

    tape = Tape.new(tape_path)
    tape.replace_all([templ])
    tape.save()
    s = settings("replay")
    s.match = MatchSettings(on=("method", "path"))
    with ReelayServer(s, Tape.load(tape_path)) as srv:
        _, _, raw1 = http("POST", srv.url + "/orders", {"sku": "Z9"})
        _, _, raw2 = http("POST", srv.url + "/orders", {"sku": "Q1"})
    assert json.loads(raw1) == {"id": 1, "sku": "Z9", "path": "/orders"}
    assert json.loads(raw2)["id"] == 2 and json.loads(raw2)["sku"] == "Q1"


def test_chunked_request_body(upstream: str, tape_path: Path) -> None:
    with ReelayServer(settings("record", upstream), Tape.open_or_create(tape_path, upstream)) as srv:
        host, port = srv.address
        payload = b'{"name":"chunky"}'
        raw = (b"POST /users HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")  # fmt: skip
        raw += f"{len(payload[:5]):x}\r\n".encode() + payload[:5] + b"\r\n"
        raw += f"{len(payload[5:]):x}\r\n".encode() + payload[5:] + b"\r\n0\r\n\r\n"
        with socket.create_connection((host, port), timeout=5) as conn:
            conn.sendall(raw)
            response = b""
            while chunk := conn.recv(65536):
                response += chunk
    assert b"201" in response.split(b"\r\n", 1)[0]
    assert Tape.load(tape_path).exchanges[0].request.body.content == {"name": "chunky"}


def test_body_size_limit(upstream: str, tape_path: Path) -> None:
    with ReelayServer(settings("record", upstream, max_body_size=100), Tape.open_or_create(tape_path)) as srv:
        assert http("POST", srv.url + "/users", {"blob": "x" * 500})[0] == 413


def test_control_api_and_dashboard(upstream: str, tape_path: Path) -> None:
    with ReelayServer(settings("hybrid", upstream), Tape.open_or_create(tape_path, upstream)) as srv:
        base = srv.url + "/__reelay"
        jget(srv.url + "/users/5")
        assert http("GET", base)[0] in (200, 308)
        status, _, html = http("GET", base + "/")
        assert status == 200 and b"reelay" in html and b"{{PREFIX}}" not in html
        assert http("GET", base + "/app.js")[1]["content-type"].startswith("text/javascript")
        assert jget(base + "/api/health")[2]["mode"] == "hybrid"
        traffic = jget(base + "/api/traffic")[2]
        assert traffic[0]["target"] == "/users/5" and traffic[0]["source"] == "recorded"
        detail = jget(base + f"/api/traffic/{traffic[0]['id']}")[2]
        assert detail["exchange"]["response"]["status"] == 200
        stats = jget(base + "/api/stats")[2]
        assert stats["total"] == 1 and stats["tape"]["exchanges"] == 1
        # CSRF guard: mutating control calls need the custom header
        assert http("POST", base + "/api/mode", {"mode": "replay"})[0] == 403
        ok = http("POST", base + "/api/mode", {"mode": "replay"}, {"X-Reelay-Control": "1"})
        assert ok[0] == 200 and srv.engine.mode == "replay"
        bad = http("POST", base + "/api/mode", {"mode": "nope"}, {"X-Reelay-Control": "1"})
        assert bad[0] == 400
        assert http("POST", base + "/api/clear", b"", {"X-Reelay-Control": "1"})[0] == 200
        assert jget(base + "/api/traffic")[2] == []
        assert jget(base + "/api/unknown")[0] == 404


def test_server_sent_events(tape_path: Path) -> None:
    tape = Tape.new(tape_path)
    tape.replace_all([ex(path="/ping")])
    tape.save()
    with ReelayServer(settings("replay"), Tape.load(tape_path)) as srv:
        lines: list[str] = []

        def listen() -> None:
            with request.urlopen(srv.url + "/__reelay/api/events", timeout=5) as resp:
                for raw in resp:
                    line = raw.decode().strip()
                    if line.startswith("data:"):
                        lines.append(line)
                        return

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        time.sleep(0.3)
        jget(srv.url + "/ping")
        thread.join(5)
    assert lines and json.loads(lines[0][5:])["target"] == "/ping"


def test_cors_mode(tape_path: Path) -> None:
    tape = Tape.new(tape_path)
    tape.replace_all([ex(path="/data")])
    tape.save()
    with ReelayServer(settings("replay", cors=True), Tape.load(tape_path)) as srv:
        _, headers, _ = jget(srv.url + "/data", headers={"Origin": "http://localhost:3000"})
        assert headers["access-control-allow-origin"] == "http://localhost:3000"
        status, headers, _ = http(
            "OPTIONS",
            srv.url + "/data",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
        )
        assert status == 204 and "GET" in headers["access-control-allow-methods"]


def test_configuration_errors(tape_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs an upstream"):
        ReelayServer(settings("record"), Tape.new(tape_path))
    with pytest.raises(ConfigError, match="needs a tape"):
        ReelayServer(settings("replay"), None)
    with ReelayServer(settings("replay"), Tape.new(tape_path)) as srv:
        _, port = srv.address
        with pytest.raises(ConfigError, match="cannot listen"):
            ReelayServer(settings("replay", port=port), Tape.new(tape_path))
        with pytest.raises(ValueError):
            srv.engine.set_mode("record")


def test_testing_helper_and_fixture_style(upstream: str, tape_path: Path) -> None:
    with replay(tape_path, target=upstream, mode="hybrid") as api:
        assert jget(api.url + "/users/42")[2]["id"] == 42
    with replay(tape_path) as api:
        assert jget(api.url + "/users/42")[2]["id"] == 42


@pytest.mark.reelay(tape="tests/tapes/demo.tape")
def test_pytest_plugin_fixture(reelay: ReelayServer) -> None:
    status, headers, body = jget(reelay.url + "/greeting?name=world")
    assert status == 200 and body == {"message": "hello, world"}
    assert headers["x-reelay"] == "replayed"
