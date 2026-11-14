"""Diffing, schema/OpenAPI inference, YAML output, HAR, chaos, templating, tape ops, config."""

from __future__ import annotations

import itertools
import json
import random
import tomllib
from typing import Any

import pytest

from reelay import jsondiff, openapi, schema, tapeops, yamlout
from reelay.bodies import Body
from reelay.chaos import ChaosEngine, ChaosRule, Latency, parse_bandwidth
from reelay.compare import compare_tapes, verify_live
from reelay.config import STARTER, parse_settings
from reelay.errors import ConfigError, UpstreamError
from reelay.har import from_har, to_har
from reelay.models import Request, Response
from reelay.redact import Redactor
from reelay.templating import TemplateContext, render_response

from .conftest import ex


class TestJsonDiff:
    def test_operations(self) -> None:
        old = {"a": 1, "b": {"c": [1, 2, 3]}, "gone": True, "t": "x", "n": 1}
        new = {"a": 2, "b": {"c": [1, 2]}, "new": None, "t": 5, "n": 1.0}
        deltas = {(d.op, d.path) for d in jsondiff.diff(old, new)}
        assert deltas == {("changed", "$.a"), ("removed", "$.b.c[2]"), ("removed", "$.gone"),
                          ("added", "$.new"), ("type", "$.t")}  # fmt: skip

    def test_ignore_and_describe(self) -> None:
        deltas = jsondiff.diff({"x": {"updated_at": 1}, "y": 1}, {"x": {"updated_at": 2}, "y": 2}, ["$..updated_at"])
        assert [d.path for d in deltas] == ["$.y"]
        assert jsondiff.Delta("type", "$.a", "s", 1).describe() == "~ $.a: string → integer"

    def test_max_deltas(self) -> None:
        assert len(jsondiff.diff(list(range(100)), list(range(100, 200)), max_deltas=10)) == 10


class TestSchema:
    def test_inference_and_merging(self) -> None:
        samples = [
            {"id": 1, "email": "a@b.co", "created": "2026-01-02T03:04:05Z", "score": 1, "tags": ["x"], "ref": None},
            {"id": 2, "email": "c@d.io", "created": "2026-02-02T03:04:05Z", "score": 2.5, "tags": [], "ref": "r",
             "extra": {"k": True}},
        ]  # fmt: skip
        s = schema.infer_many(samples)
        props = s["properties"]
        assert s["type"] == "object"
        assert set(s["required"]) == {"id", "email", "created", "score", "tags", "ref"}
        assert props["email"] == {"type": "string", "format": "email"}
        assert props["created"]["format"] == "date-time"
        assert props["score"]["type"] == "number"
        assert props["ref"]["type"] == ["string", "null"]
        assert props["tags"] == {"type": "array", "items": {"type": "string"}}

    def test_mixed_types_use_anyof(self) -> None:
        s = schema.infer_many([1, "a", {"k": 1}])
        assert {v["type"] for v in s["anyOf"]} == {"integer", "string", "object"}

    def test_schema_validates_samples(self) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        samples: list[Any] = [{"a": 1, "b": [1, "x"], "c": None}, {"a": 2.5, "b": [], "c": {"d": "e"}}, {"a": 3}]
        s = schema.infer_many(samples)
        for sample in samples:
            jsonschema.validate(sample, s)


class TestOpenAPI:
    def test_path_templating(self) -> None:
        paths = ["/users/1", "/users/2", "/users/3/posts", "/orgs/acme/repos", "/orgs/beta/repos",
                 "/orgs/gamma/repos", "/health", "/items/550e8400-e29b-41d4-a716-446655440000"]  # fmt: skip
        mapping = openapi.template_paths(paths)
        assert mapping["/users/1"] == "/users/{userId}"
        assert mapping["/users/3/posts"] == "/users/{userId}/posts"
        assert mapping["/orgs/acme/repos"] == "/orgs/{orgId}/repos"
        assert mapping["/health"] == "/health"
        assert mapping["/items/550e8400-e29b-41d4-a716-446655440000"] == "/items/{itemId}"

    def test_build_document(self) -> None:
        exchanges = [
            ex(path="/users/1", query=[("expand", "true")], resp={"id": 1, "name": "a"}),
            ex(path="/users/2", resp={"id": 2, "name": "b"}),
            ex(path="/users/404", status=404, resp={"error": "nope"}),
            ex("POST", "/users", body={"name": "c"}, status=201, resp={"id": 3, "name": "c"}),
        ]
        doc = openapi.build(exchanges, title="Users", server="https://api.example.com")
        assert doc["openapi"] == "3.1.0" and doc["servers"][0]["url"] == "https://api.example.com"
        get = doc["paths"]["/users/{userId}"]["get"]
        params = {p["name"]: p for p in get["parameters"]}
        assert params["userId"]["in"] == "path" and params["userId"]["schema"] == {"type": "integer"}
        assert params["expand"]["required"] is False and params["expand"]["schema"] == {"type": "boolean"}
        assert set(get["responses"]) == {"200", "404"}
        post = doc["paths"]["/users"]["post"]
        assert post["requestBody"]["content"]["application/json"]["schema"]["properties"]["name"]["type"] == "string"
        assert post["operationId"] == "postUsers"

    def test_yaml_roundtrip(self) -> None:
        yaml = pytest.importorskip("yaml")
        doc = openapi.build([ex(path="/a/1", resp={"yes": "no", "n": "1.0", "s": "a: b", "e": [], "o": {},
                                                    "multi": "x\ny", "null": None})])  # fmt: skip
        assert yaml.safe_load(yamlout.dump(doc)) == json.loads(json.dumps(doc))
        tricky: Any = {"list": [{"a": 1, "b": [1, {"c": "d"}]}, [1, 2], "on", 3.5, True], "empty": {}}
        assert yaml.safe_load(yamlout.dump(tricky)) == tricky


class TestHar:
    def test_roundtrip(self) -> None:
        tape = [ex(path="/users/1", query=[("a", "b")]), ex("POST", "/users", body={"x": 1}, status=201)]
        har = to_har(tape, "https://api.example.com")
        assert har["log"]["entries"][0]["request"]["url"] == "https://api.example.com/users/1?a=b"
        back, upstream = from_har(json.loads(json.dumps(har)))
        assert upstream == "https://api.example.com"
        assert [(e.request.method, e.request.target, e.response.status) for e in back] == [
            ("GET", "/users/1?a=b", 200),
            ("POST", "/users", 201),
        ]
        assert back[1].request.body.content == {"x": 1}

    def test_binary_and_errors(self) -> None:
        har = {"log": {"entries": [{"request": {"method": "GET", "url": "http://h/img", "headers": []},
                                    "response": {"status": 200, "headers": [{"name": "Content-Type", "value": "image/png"}],
                                                 "content": {"text": "iVBORw0K", "encoding": "base64"}}}]}}  # fmt: skip
        [e], _ = from_har(har)
        assert e.response.body.encoding == "base64"
        from reelay.errors import TapeError

        with pytest.raises(TapeError):
            from_har({"nope": 1})
        with pytest.raises(TapeError):
            from_har({"log": {"entries": [{"request": {}}]}})


class TestCompare:
    def test_tape_diff(self) -> None:
        old = [ex(path="/a", resp={"v": 1, "updated_at": 1}), ex(path="/b"), ex(path="/c")]
        new = [ex(path="/a", resp={"v": 2, "updated_at": 2}), ex(path="/b"), ex(path="/d")]
        cmp = compare_tapes(old, new, ignore=["$..updated_at"])
        assert [p.label for p in cmp.changed] == ["GET /a"]
        assert [d.path for d in cmp.changed[0].deltas] == ["$.v"]
        assert cmp.only_old == ["GET /c"] and cmp.only_new == ["GET /d"]
        assert compare_tapes(old, old).has_differences is False

    def test_status_and_header_changes(self) -> None:
        old = [ex(path="/a")]
        new_resp = Response(500, [("Content-Type", "text/html")], Body("text", "boom"))
        new = [ex(path="/a").with_(response=new_resp)]
        [pair] = compare_tapes(old, new).changed
        assert pair.old_status == 200 and pair.new_status == 500
        assert any("content-type" in n for n in pair.notes)

    def test_verify_only_safe_methods(self) -> None:
        sent: list[str] = []

        def send(e):  # type: ignore[no-untyped-def]
            sent.append(e.label)
            if e.request.path == "/down":
                raise UpstreamError("refused")
            return e.response

        cmp = verify_live([ex(path="/ok"), ex("DELETE", "/ok"), ex(path="/down")], send)
        assert sent == ["GET /ok", "GET /down"]
        assert [p.label for p in cmp.changed] == ["GET /down"]


class TestChaos:
    def test_latency_specs(self) -> None:
        rng = random.Random(1)
        assert Latency.parse("250ms").sample(rng) == 0.25
        uni = Latency.parse("100ms..300ms")
        assert all(0.1 <= uni.sample(rng) <= 0.3 for _ in range(50))
        assert Latency.parse("normal(1s, 0.1s)").kind == "normal"
        assert Latency.parse(0.5).a == 0.5
        with pytest.raises(ValueError):
            Latency.parse("300ms..100ms")
        assert parse_bandwidth("64KiB/s") == 65536

    def test_deterministic_decisions(self) -> None:
        rules = [ChaosRule("GET /flaky/**", error_rate=0.5, error_status=503), ChaosRule("/**", drop_rate=0.0)]
        a = ChaosEngine(rules, seed=7)
        b = ChaosEngine(rules, seed=7)
        da = [a.decide("GET", "/flaky/x").error_status for _ in range(40)]
        db = [b.decide("GET", "/flaky/x").error_status for _ in range(40)]
        assert da == db and 5 < da.count(503) < 35
        assert not a.decide("POST", "/other").active
        assert not ChaosEngine(rules, seed=1, enabled=False).decide("GET", "/flaky/x").active

    @pytest.mark.parametrize("kw", [{"error_rate": 2}, {"error_status": 200}, {"route": "bad"}])
    def test_rule_validation(self, kw: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            ChaosRule(**kw)


class TestTemplating:
    def ctx(self) -> TemplateContext:
        req = Request("POST", "/orders/77", [("currency", "EUR")], [("X-Trace", "t-1")],
                      Body("json", {"items": [{"sku": "A1"}], "count": 3}))  # fmt: skip
        return TemplateContext(req, itertools.count(1), random.Random(0))

    def test_expressions(self) -> None:
        c = self.ctx()
        assert c.render_text("{{request.method}} {{ request.path.1 }} {{request.query.currency}}") == "POST 77 EUR"
        assert c.render_text("{{request.header.x-trace}}/{{request.json.items.0.sku}}") == "t-1/A1"
        assert c.render_text("{{seq}},{{seq}}") == "1,2"
        assert c.render_text("{{unknown.thing}}!") == "!"
        assert 1 <= int(c.render_text("{{random_int(1,6)}}")) <= 6
        assert len(c.render_text("{{uuid}}")) == 36

    def test_json_keeps_types(self) -> None:
        resp = Response(201, [("Location", "/orders/{{request.path.1}}")],
                        Body("json", {"count": "{{request.json.count}}", "label": "n={{request.json.count}}",
                                      "missing": "{{request.json.nope}}"}))  # fmt: skip
        out = render_response(resp, self.ctx())
        assert out.body.content == {"count": 3, "label": "n=3", "missing": None}
        assert out.headers == [("Location", "/orders/77")]


class TestTapeOps:
    def test_stats_filter_merge_redact(self) -> None:
        tape = [ex(path="/users/1", duration_ms=10), ex(path="/users/2", duration_ms=30),
                ex(path="/users/3", status=404, duration_ms=5), ex("POST", "/users", body={"a": 1}),
                ex(path="/health")]  # fmt: skip
        stats = {(s.method, s.path): s for s in tapeops.endpoint_stats(tape)}
        assert stats[("GET", "/users/{userId}")].count == 3
        assert stats[("GET", "/users/{userId}")].statuses == {200: 2, 404: 1}
        assert len(tapeops.filter_exchanges(tape, include=["GET /users/*"])) == 3
        assert len(tapeops.filter_exchanges(tape, exclude=["/health"], status=[200])) == 3
        merged = tapeops.merge([tape, tape])
        assert len(merged) == 10 and len(tapeops.merge([tape, tape], dedupe=True)) == 5
        leaky = [ex(headers=[("Authorization", "Bearer abcdefghijkl")])]
        cleaned, changed = tapeops.redact_all(leaky, Redactor())
        assert changed == 1 and cleaned[0].request.headers[0][1] == "[REDACTED]"


class TestConfig:
    def test_starter_is_valid(self) -> None:
        s = parse_settings(tomllib.loads(STARTER))
        assert s.server.mode == "hybrid" and s.chaos.rules[0].error_rate == 0.05
        assert s.match.ignore_body == ["$.requestId", "$..timestamp"]
        assert "x-internal-token" in s.redact.headers

    @pytest.mark.parametrize(("data", "message"), [
        ({"server": {"mode": "live"}}, "must be one of"),
        ({"server": {"prot": 1}}, "did you mean 'port'"),
        ({"server": {"port": 70000}}, "between 0 and 65535"),
        ({"server": {"timeout": "soon"}}, "invalid duration"),
        ({"server": {"control_prefix": "admin"}}, "control_prefix"),
        ({"match": {"on": ["colour"]}}, "unknown match dimension"),
        ({"match": {"ignore_body": ["nope"]}}, "JSONPath"),
        ({"redact": {"patterns": ["(x"]}}, "invalid pattern"),
        ({"chaos": {"rule": [{"latency": "fast"}]}}, "invalid duration"),
        ({"chaos": {"rule": [{"error_rate": 3}]}}, "between 0 and 1"),
        ({"chaos": {"rule": [{"bandwidth": "lots"}]}}, "bandwidth"),
        ({"unknown": {}}, "unknown key"),
    ])  # fmt: skip
    def test_errors(self, data: dict[str, Any], message: str) -> None:
        with pytest.raises(ConfigError) as info:
            parse_settings(data)
        assert message in info.value.message or message in (info.value.hint or "")
