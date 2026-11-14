"""JSONPath, redaction and the matching engine."""

from __future__ import annotations

import re

import pytest

from reelay import jsonpath
from reelay.bodies import Body
from reelay.matching import Matcher, MatchSettings
from reelay.models import Request
from reelay.redact import RedactionRules, Redactor

from .conftest import ex

DOC = {"user": {"id": 1, "tags": ["a", "b"], "meta": {"updated_at": "x"}},
       "items": [{"id": 1, "updated_at": "t1"}, {"id": 2, "updated_at": "t2"}], "odd key": 3}  # fmt: skip


class TestJsonPath:
    @pytest.mark.parametrize(("path", "values"), [
        ("$.user.id", [1]),
        ("$.user.tags[1]", ["b"]),
        ("$.user.tags[-1]", ["b"]),
        ("$.items[*].id", [1, 2]),
        ("$..updated_at", ["x", "t1", "t2"]),
        ("$['odd key']", [3]),
        ("$.missing", []),
        ("$.items.*.id", [1, 2]),
    ])  # fmt: skip
    def test_find(self, path: str, values: list[object]) -> None:
        assert [v for _, v in jsonpath.find(DOC, path)] == values

    def test_matches_concrete_locations(self) -> None:
        assert jsonpath.matches("$..updated_at", ("items", 0, "updated_at"))
        assert jsonpath.matches("$.items[*].id", ("items", 5, "id"))
        assert not jsonpath.matches("$.items[0].id", ("items", 1, "id"))
        assert jsonpath.matches("$.user", ("user",))
        assert not jsonpath.matches("$.user", ("user", "id"))

    def test_delete_and_assign(self) -> None:
        import copy

        doc = copy.deepcopy(DOC)
        jsonpath.delete(doc, "$..updated_at")
        assert "updated_at" not in doc["items"][0] and "updated_at" not in doc["user"]["meta"]
        doc2 = {"list": [1, 2, 3, 4]}
        jsonpath.delete(doc2, "$.list[*]")
        assert doc2 == {"list": []}
        assert jsonpath.assign(doc, "$.items[*].id", 0) == 2

    def test_location_str(self) -> None:
        assert jsonpath.location_str(("a", 0, "b c")) == "$.a[0]['b c']"

    @pytest.mark.parametrize("bad", ["user.id", "$.[", "$..", "$['unterminated]"])
    def test_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            jsonpath.compile_path(bad)


class TestRedaction:
    def test_headers_query_json_and_patterns(self) -> None:
        r = Redactor()
        req = Request("POST", "/login", [("api_key", "abc"), ("page", "2")],
                      [("Authorization", "Bearer secret"), ("X-Note", "token ghp_" + "a" * 36)],
                      Body("json", {"user": "me", "password": "hunter2", "nested": {"Token": "t"},
                                    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk"}))  # fmt: skip
        out = r.request(req)
        assert out.query == [("api_key", "[REDACTED]"), ("page", "2")]
        assert out.headers[0] == ("Authorization", "[REDACTED]")
        assert "ghp_" not in out.headers[1][1]
        assert out.body.content["password"] == "[REDACTED]"
        assert out.body.content["nested"]["Token"] == "[REDACTED]"
        assert out.body.content["jwt"] == "[REDACTED]"
        assert out.body.content["user"] == "me"
        assert req.body.content["password"] == "hunter2"  # original untouched

    def test_form_bodies_and_text(self) -> None:
        r = Redactor()
        form = Request("POST", "/t", [], [("Content-Type", "application/x-www-form-urlencoded")],
                       Body("text", "client_secret=s3&grant_type=x"))  # fmt: skip
        assert r.request(form).body.content == "client_secret=%5BREDACTED%5D&grant_type=x"
        text = Body("text", "key AKIAABCDEFGHIJKLMNOP end")
        assert r.body(text, "text/plain").content == "key [REDACTED] end"

    def test_custom_rules_and_paths(self) -> None:
        rules = RedactionRules.build(extra_headers=["X-Internal"], extra_json_keys=["ssn"],
                                     json_paths=["$.card.number"], extra_patterns=[r"acct_\d+"],
                                     placeholder="***")  # fmt: skip
        r = Redactor(rules)
        resp_body = r.json({"ssn": "1", "card": {"number": "4111"}, "note": "acct_42 paid"})
        assert resp_body == {"ssn": "***", "card": {"number": "***"}, "note": "*** paid"}
        assert r.headers([("x-internal", "v")]) == [("x-internal", "***")]

    def test_disabled(self) -> None:
        r = Redactor(RedactionRules.build(enabled=False))
        req = Request("GET", "/", [("token", "x")])
        assert r.request(req) is req

    def test_invalid_rules(self) -> None:
        with pytest.raises(re.error):
            RedactionRules.build(extra_patterns=["(unclosed"])
        with pytest.raises(ValueError):
            RedactionRules.build(json_paths=["no-dollar"])


class TestMatcher:
    def test_exact_match_ignores_query_order_and_secrets(self) -> None:
        recorded = Redactor().exchange(ex(query=[("a", "1"), ("token", "old")]))
        m = Matcher([recorded])
        hit = m.find(Request("GET", "/users/1", [("token", "new"), ("a", "1")]))
        assert hit.kind == "exact" and hit.exchange is recorded

    def test_sequential_cycle_first_last(self) -> None:
        a, b, c = ex(resp={"n": 1}), ex(resp={"n": 2}), ex(resp={"n": 3})
        req = Request("GET", "/users/1")
        seq = Matcher([a, b, c])
        assert [seq.find(req).exchange for _ in range(5)] == [a, b, c, c, c]
        seq.reset()
        assert seq.find(req).exchange is a
        cyc = Matcher([a, b], MatchSettings(strategy="cycle"))
        assert [cyc.find(req).exchange for _ in range(3)] == [a, b, a]
        assert Matcher([a, b], MatchSettings(strategy="first")).find(req).exchange is a
        assert Matcher([a, b], MatchSettings(strategy="last")).find(req).exchange is b

    def test_body_matching_with_ignored_fields(self) -> None:
        recorded = ex("POST", "/users", body={"name": "a", "requestId": "r1", "meta": {"ts": 1}})
        m = Matcher([recorded], MatchSettings(ignore_body=["$.requestId", "$..ts"]))
        req = Request("POST", "/users", [], [("Content-Type", "application/json")],
                      Body("json", {"name": "a", "requestId": "r2", "meta": {"ts": 2}}))  # fmt: skip
        assert m.find(req).kind == "exact"
        other = Request("POST", "/users", [], [], Body("json", {"name": "b"}))
        miss = m.find(other)
        assert miss.kind == "miss"
        assert any("body differs at $.name" in r for r in miss.candidates[0].reasons)

    def test_trailing_slash_and_ignored_query(self) -> None:
        m = Matcher([ex(path="/items", query=[("page", "1")])], MatchSettings(ignore_query=frozenset({"_"})))
        assert m.find(Request("GET", "/items/", [("page", "1"), ("_", "123")])).hit
        strict = Matcher([ex(path="/items")], MatchSettings(trailing_slash=True))
        assert not strict.find(Request("GET", "/items/")).hit

    def test_header_dimension(self) -> None:
        settings = MatchSettings(on=("method", "path", "headers"), headers=("Accept",))
        json_ex = ex(headers=[("Accept", "application/json")], resp={"kind": "json"})
        csv_ex = ex(headers=[("Accept", "text/csv")], resp={"kind": "csv"})
        m = Matcher([json_ex, csv_ex], settings)
        assert m.find(Request("GET", "/users/1", [], [("accept", "text/csv")])).exchange is csv_ex
        miss = m.find(Request("GET", "/users/1", [], [("Accept", "text/xml")]))
        assert "header differs: accept" in miss.candidates[0].reasons

    def test_miss_explanations_and_fuzzy(self) -> None:
        recorded = ex(path="/search", query=[("q", "cats"), ("page", "1")])
        m = Matcher([recorded])
        miss = m.find(Request("GET", "/search", [("q", "dogs"), ("page", "1")]))
        assert miss.kind == "miss" and miss.candidates[0].reasons == ("query differs: q",)
        fuzzy = Matcher([recorded], MatchSettings(fuzzy=True)).find(Request("GET", "/search", [("q", "dogs")]))
        assert fuzzy.kind == "fuzzy" and fuzzy.exchange is recorded

    def test_add_at_runtime(self) -> None:
        m = Matcher()
        assert not m.find(Request("GET", "/x")).hit
        m.add(ex(path="/x"))
        assert m.find(Request("GET", "/x")).hit and len(m) == 1

    def test_invalid_settings(self) -> None:
        with pytest.raises(ValueError):
            MatchSettings(on=("colour",))
        with pytest.raises(ValueError):
            MatchSettings(strategy="random")
