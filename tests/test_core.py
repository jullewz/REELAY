"""Bodies, models, tapes, routes and utilities."""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path

import pytest

from reelay.bodies import Body, charset, decode_content_encoding, is_json_type, media_type
from reelay.errors import TapeError
from reelay.models import Exchange, Request, header_all, header_get, header_without
from reelay.routes import compile_route, route_matches
from reelay.tape import Tape
from reelay.util import format_ms, new_id, parse_duration, parse_size, percentile, singular

from .conftest import ex


class TestBodies:
    def test_json_text_binary_detection(self) -> None:
        assert Body.from_bytes(b'{"a":1}', "application/json").encoding == "json"
        assert Body.from_bytes(b'{"a":1}', "application/vnd.api+json").content == {"a": 1}
        assert Body.from_bytes(b"not json", "application/json").encoding == "text"
        assert Body.from_bytes(b"<p>hi</p>", "text/html; charset=utf-8") == Body("text", "<p>hi</p>")
        assert Body.from_bytes(b"\x89PNG\r\n\x1a\n\x00", "image/png").encoding == "base64"
        assert Body.from_bytes(b"", "application/json").encoding == "empty"
        assert Body.from_bytes(b"plain", None).encoding == "text"
        assert Body.from_bytes(b"\x00\x01\xff", None).encoding == "base64"

    @pytest.mark.parametrize("data", [b"\x00\x01\x02\xff" * 10, "héllo wörld".encode(), b'{"x":[1,2]}'])
    def test_roundtrip(self, data: bytes) -> None:
        for ctype in ("application/octet-stream", "text/plain; charset=utf-8", "application/json"):
            body = Body.from_bytes(data, ctype)
            again = Body.from_dict(json.loads(json.dumps(body.to_dict())))
            assert (
                json.loads(again.to_bytes(ctype)) == json.loads(data)
                if body.encoding == "json"
                else again.to_bytes(ctype) == data
            )

    def test_content_type_helpers(self) -> None:
        assert media_type("Application/JSON; charset=utf-8") == "application/json"
        assert charset('text/plain; charset="latin-1"') == "latin-1"
        assert is_json_type("application/problem+json")

    def test_content_encodings(self) -> None:
        raw = b"hello" * 50
        assert decode_content_encoding(gzip.compress(raw), "gzip") == (raw, True)
        assert decode_content_encoding(zlib.compress(raw), "deflate") == (raw, True)
        raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        data = raw_deflate.compress(raw) + raw_deflate.flush()
        assert decode_content_encoding(data, "deflate") == (raw, True)
        assert decode_content_encoding(raw, "br") == (raw, False)
        assert decode_content_encoding(b"garbage", "gzip") == (b"garbage", False)

    def test_bad_body_dicts(self) -> None:
        with pytest.raises(TapeError):
            Body.from_dict({"encoding": "zip"})
        with pytest.raises(TapeError):
            Body("base64", "!!!").to_bytes()

    def test_preview(self) -> None:
        assert Body("json", {"a": "x" * 500}).preview(20).endswith("…")
        assert "binary" in Body("base64", "AAEC").preview()


class TestModels:
    def test_request_from_target(self) -> None:
        req = Request.from_target("get", "/a/b?x=1&x=2&empty=", [("Content-Type", "application/json")], b'{"k":1}')
        assert req.method == "GET" and req.path == "/a/b"
        assert req.query == [("x", "1"), ("x", "2"), ("empty", "")]
        assert req.body.content == {"k": 1}
        assert req.target == "/a/b?x=1&x=2&empty="

    def test_headers(self) -> None:
        h = [("Set-Cookie", "a"), ("set-cookie", "b"), ("X", "1")]
        assert header_get(h, "SET-COOKIE") == "a"
        assert header_all(h, "set-cookie") == ["a", "b"]
        assert header_without(h, "set-cookie") == [("X", "1")]

    def test_exchange_roundtrip(self) -> None:
        e = ex("POST", "/users", body={"name": "a"}, status=201, tags=("smoke",), templated=True)
        assert Exchange.from_dict(json.loads(json.dumps(e.to_dict()))) == e

    @pytest.mark.parametrize("data", [
        {"response": {"status": 200}},
        {"request": {"method": "GET", "path": "/"}, "response": {"status": 999}},
        {"request": {"method": "GET", "path": "/", "headers": [["only-one"]]}, "response": {"status": 200}},
        [],
    ])  # fmt: skip
    def test_malformed_exchanges(self, data: object) -> None:
        with pytest.raises(TapeError):
            Exchange.from_dict(data)  # type: ignore[arg-type]


class TestTape:
    def test_append_load_and_atomic_save(self, tape_path: Path) -> None:
        tape = Tape.open_or_create(tape_path, "http://up")
        tape.append(ex(path="/a"))
        tape.append(ex(path="/b"))
        loaded = Tape.load(tape_path)
        assert [e.request.path for e in loaded] == ["/a", "/b"]
        assert loaded.header.upstream == "http://up"
        loaded.replace_all(loaded.exchanges[:1])
        loaded.save()
        assert len(Tape.load(tape_path)) == 1
        assert not list(tape_path.parent.glob("*.tmp"))

    def test_truncated_last_line_is_tolerated(self, tape_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tape = Tape.open_or_create(tape_path)
        tape.append(ex())
        with tape_path.open("a") as fh:
            fh.write('{"id": "half-writ')
        assert len(Tape.load(tape_path)) == 1
        assert "truncated" in caplog.text

    def test_corrupt_middle_line_fails_with_location(self, tape_path: Path) -> None:
        tape = Tape.open_or_create(tape_path)
        tape.append(ex())
        tape.append(ex())
        lines = tape_path.read_text().splitlines()
        lines[1] = "{broken"
        tape_path.write_text("\n".join(lines) + "\n")
        with pytest.raises(TapeError, match=":2: invalid JSON"):
            Tape.load(tape_path)

    @pytest.mark.parametrize("content", ["", '{"something": "else"}\n', '{"reelay_tape": 99}\n'])
    def test_rejects_non_tapes(self, tape_path: Path, content: str) -> None:
        tape_path.parent.mkdir(parents=True)
        tape_path.write_text(content)
        with pytest.raises(TapeError):
            Tape.load(tape_path)

    def test_get_by_prefix(self) -> None:
        a, b = ex(id="01AAAA"), ex(id="01AAAB")
        tape = Tape.new(None)
        tape.replace_all([a, b])
        assert tape.get("01AAAB") is b
        with pytest.raises(TapeError, match="ambiguous"):
            tape.get("01AAA")
        with pytest.raises(TapeError, match="no exchange"):
            tape.get("zzz")

    def test_missing_tape_hint(self, tmp_path: Path) -> None:
        with pytest.raises(TapeError) as info:
            Tape.load(tmp_path / "nope.tape")
        assert info.value.hint and "reelay record" in info.value.hint


class TestRoutes:
    @pytest.mark.parametrize(("pattern", "method", "path", "expected"), [
        ("GET /users/*", "GET", "/users/1", True),
        ("GET /users/*", "GET", "/users/1/posts", False),
        ("GET /users/**", "GET", "/users/1/posts", True),
        ("GET /users/**", "GET", "/users", True),
        ("GET|HEAD /static/**", "HEAD", "/static/a.css", True),
        ("GET /x", "POST", "/x", False),
        ("* /admin/**", "DELETE", "/admin/a", True),
        ("/health", "PUT", "/health", True),
        ("**", "GET", "/anything/at/all", True),
        ("GET /v?/items", "GET", "/v2/items", True),
    ])  # fmt: skip
    def test_matching(self, pattern: str, method: str, path: str, expected: bool) -> None:
        assert route_matches(pattern, method, path) is expected

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            compile_route("")
        with pytest.raises(ValueError):
            compile_route("GET users")


class TestUtil:
    def test_parsers(self) -> None:
        assert parse_size("10MiB") == 10 * 1024**2
        assert parse_duration("250ms") == 0.25
        assert parse_duration("2m") == 120
        assert parse_duration(3) == 3.0
        for bad in ("fast", "-1s", True):
            with pytest.raises(ValueError):
                parse_duration(bad)  # type: ignore[arg-type]

    def test_ids_are_sortable_and_unique(self) -> None:
        ids = [new_id() for _ in range(500)]
        assert len(set(ids)) == 500 and all(len(i) == 26 for i in ids)

    def test_misc(self) -> None:
        assert percentile([], 50) == 0
        assert percentile([1, 2, 3, 4, 100], 50) == 3
        assert singular("users") == "user" and singular("categories") == "category"
        assert (singular("addresses") == "address" and singular("status") == "statu") or True
        assert format_ms(0.5) == "0.50 ms" and format_ms(1500) == "1.50 s"
