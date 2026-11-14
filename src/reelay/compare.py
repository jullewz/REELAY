"""Compare traffic: tape vs tape (``reelay diff``) and tape vs live API (``reelay verify``).

Both produce a :class:`Comparison`: exchanges are paired by their match
key (method, path, query, body), then status, selected headers and bodies
are diffed. JSON bodies get a structural diff with JSONPath locations,
volatile fields can be ignored with ``--ignore '$..updated_at'``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from reelay import jsondiff
from reelay.bodies import media_type
from reelay.errors import UpstreamError
from reelay.matching import Matcher, MatchSettings
from reelay.models import Exchange, Response, header_get
from reelay.redact import Redactor

COMPARED_HEADERS = ("content-type", "location", "cache-control")


@dataclass(slots=True)
class PairResult:
    label: str
    old_status: int | None
    new_status: int | None
    deltas: list[jsondiff.Delta] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.deltas or self.notes or self.old_status != self.new_status)

    def to_dict(self) -> dict[str, Any]:
        return {"request": self.label, "old_status": self.old_status, "new_status": self.new_status,
                "notes": self.notes, "deltas": [d.to_dict() for d in self.deltas]}  # fmt: skip


@dataclass(slots=True)
class Comparison:
    title: str
    pairs: list[PairResult] = field(default_factory=list)
    only_old: list[str] = field(default_factory=list)
    only_new: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[PairResult]:
        return [p for p in self.pairs if p.changed]

    @property
    def has_differences(self) -> bool:
        return bool(self.changed or self.only_old or self.only_new)

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "compared": len(self.pairs), "changed": [p.to_dict() for p in self.changed],
                "only_old": self.only_old, "only_new": self.only_new,
                "has_differences": self.has_differences}  # fmt: skip


def compare_responses(label: str, old: Response, new: Response, ignore: list[str]) -> PairResult:
    result = PairResult(label, old.status, new.status)
    for name in COMPARED_HEADERS:
        a, b = header_get(old.headers, name), header_get(new.headers, name)
        if name == "content-type":
            a, b = media_type(a) or None, media_type(b) or None
        if a != b:
            result.notes.append(f"header {name}: {a!r} → {b!r}")
    ob, nb = old.body, new.body
    if ob.encoding == nb.encoding == "json":
        result.deltas = jsondiff.diff(ob.content, nb.content, ignore)
    elif ob.encoding != nb.encoding:
        result.notes.append(f"body encoding {ob.encoding} → {nb.encoding}")
    elif ob.content != nb.content:
        result.notes.append(f"{ob.encoding} body differs ({ob.size} → {nb.size} bytes)")
    return result


def compare_tapes(old: Iterable[Exchange], new: Iterable[Exchange], *, ignore: Iterable[str] = (),
                  settings: MatchSettings | None = None, title: str = "tape diff") -> Comparison:  # fmt: skip
    matcher = Matcher(settings=settings or MatchSettings(), redactor=Redactor())
    groups_old: dict[str, list[Exchange]] = defaultdict(list)
    groups_new: dict[str, list[Exchange]] = defaultdict(list)
    for ex in old:
        groups_old[matcher.key(ex.request, redacted=True)].append(ex)
    for ex in new:
        groups_new[matcher.key(ex.request, redacted=True)].append(ex)
    ignore_list = list(ignore)
    out = Comparison(title)
    for key, olds in groups_old.items():
        news = groups_new.get(key, [])
        for i, ex in enumerate(olds):
            if i < len(news):
                out.pairs.append(compare_responses(ex.label, ex.response, news[i].response, ignore_list))
            else:
                out.only_old.append(ex.label)
    for key, news in groups_new.items():
        extra = news[len(groups_old.get(key, [])) :]
        out.only_new.extend(ex.label for ex in extra)
    return out


def verify_live(exchanges: Iterable[Exchange], send: Callable[[Exchange], Response], *,
                ignore: Iterable[str] = (), methods: Iterable[str] = ("GET", "HEAD", "OPTIONS"),
                title: str = "contract verification") -> Comparison:  # fmt: skip
    """Re-send recorded requests and diff live responses against the tape.

    Only ``methods`` (safe methods by default) are replayed against the live
    server, so verification never creates or deletes real data by accident.
    """
    allowed = {m.upper() for m in methods}
    ignore_list = list(ignore)
    out = Comparison(title)
    for ex in exchanges:
        if ex.request.method not in allowed:
            continue
        try:
            live = send(ex)
        except UpstreamError as exc:
            pair = PairResult(ex.label, ex.response.status, None, notes=[exc.message])
        else:
            pair = compare_responses(ex.label, ex.response, live, ignore_list)
        out.pairs.append(pair)
    return out
