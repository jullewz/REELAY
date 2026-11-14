"""Command-line interface.

Exit codes: 0 success, 1 differences found (diff, verify), 2 usage or
configuration error, 4 internal error.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from reelay import __author__, __url__, __version__, openapi, tapeops, yamlout
from reelay.chaos import ChaosEngine
from reelay.compare import Comparison, compare_tapes, verify_live
from reelay.config import CONFIG_FILENAME, MODES, ON_MISS, STARTER, Settings, load_settings
from reelay.errors import EXIT_DIFFERENCES, EXIT_INTERNAL, EXIT_OK, EXIT_USAGE, ReelayError, UsageError
from reelay.har import from_har, to_har
from reelay.matching import STRATEGIES
from reelay.models import Exchange, Response
from reelay.redact import Redactor
from reelay.server import ReelayServer
from reelay.tape import Tape
from reelay.term import Style
from reelay.upstream import Target, UpstreamClient
from reelay.util import format_ms, format_size

log = logging.getLogger("reelay.cli")
Handler = Callable[[argparse.Namespace], int]


# ------------------------------------------------------------------- helpers
def _style(args: argparse.Namespace) -> Style:
    return Style(sys.stdout, color=False if args.no_color else None)


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(args.config)


def _write(text: str, output: str | None, what: str) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {what} to {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _load(path: str) -> Tape:
    return Tape.load(Path(path))


def _setup_logging(verbosity: int) -> None:
    level = {-1: logging.ERROR, 0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
    logger = logging.getLogger("reelay")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("reelay: %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


# --------------------------------------------------------------------- serve
def _apply_server_args(s: Settings, args: argparse.Namespace, mode: str | None) -> None:
    srv = s.server
    if mode:
        srv.mode = mode
    for attr in ("host", "port", "target", "on_miss"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(srv, attr, value)
    if getattr(args, "tape", None):
        srv.tape = Path(args.tape).expanduser().resolve()
    if getattr(args, "insecure", False):
        srv.verify_tls = False
    if getattr(args, "no_dashboard", False):
        srv.dashboard = False
    if getattr(args, "cors", False):
        srv.cors = True
    if getattr(args, "strategy", None):
        s.match.strategy = args.strategy
    if getattr(args, "fuzzy", False):
        s.match.fuzzy = True
    if getattr(args, "chaos", None) is not None:
        s.chaos.enabled = args.chaos
    if getattr(args, "seed", None) is not None:
        s.chaos = ChaosEngine(s.chaos.rules, args.seed, s.chaos.enabled)


def _serve(args: argparse.Namespace, mode: str | None) -> int:
    st = _style(args)
    s = _settings(args)
    _apply_server_args(s, args, mode)
    srv = s.server
    if srv.mode not in MODES:
        raise UsageError(f"unknown mode {srv.mode!r}")
    tape: Tape | None = None
    if srv.tape:
        creating = srv.mode in ("record", "hybrid") or srv.on_miss == "record"
        tape = Tape.open_or_create(srv.tape, srv.target) if creating else Tape.load(srv.tape)
    server = ReelayServer(s, tape)
    if srv.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("listening on %s: anyone who can reach this address can use the proxy and dashboard", srv.host)

    print(st.rule(f"reelay {__version__}"))
    rows = [("mode", st(srv.mode, "accent")), ("listening", st(server.url, "accent_strong"))]
    if srv.target:
        rows.append(("upstream", srv.target))
    if tape is not None:
        rows.append(("tape", f"{tape.path}  ({len(tape)} exchanges)"))
    if srv.dashboard:
        rows.append(("dashboard", st(f"{server.url}{srv.control_prefix}/", "accent")))
    if s.chaos.enabled and s.chaos.rules:
        rows.append(("chaos", f"{len(s.chaos.rules)} rule(s), seed {s.chaos.seed}"))
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {st(key.ljust(width), 'muted')}  {value}")
    print(st("  press Ctrl+C to stop", "muted"))
    print()
    sys.stdout.flush()

    stop = threading.Event()
    if not args.quiet:
        sub = server.engine.bus.subscribe()

        def printer() -> None:
            while not stop.is_set():
                try:
                    ev = sub.get(timeout=0.3)
                except queue.Empty:
                    continue
                if ev is None:
                    return
                extra = " ".join(x for x in (ev.match if ev.match not in (None, "exact") else None, ev.chaos, ev.note)
                                 if x)  # fmt: skip
                line = (f"  {st(ev.at[11:19], 'muted')} {st(f'{ev.source:<9}', ev.source)} "
                        f"{st(f'{ev.method:<6}', ev.method)} {st.status(ev.status)} "
                        f"{st(f'{format_ms(ev.duration_ms):>8}', 'muted')}  {ev.target}"
                        + (f"  {st(extra, 'muted')}" if extra else ""))  # fmt: skip
                print(line, flush=True)

        threading.Thread(target=printer, daemon=True, name="reelay-printer").start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        server.close()
    stats = server.engine.bus.stats()
    summary = ", ".join(f"{n} {k}" for k, n in sorted(stats["by_source"].items())) or "no requests"
    print(st(f"stopped: {summary}", "muted"))
    if tape is not None and srv.mode in ("record", "hybrid"):
        print(st(f"tape {tape.path} now holds {len(tape)} exchanges", "muted"))
    return EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    return _serve(args, "record")


def cmd_replay(args: argparse.Namespace) -> int:
    return _serve(args, "replay")


def cmd_serve(args: argparse.Namespace) -> int:
    return _serve(args, args.mode)


# ---------------------------------------------------------------- inspection
def cmd_ls(args: argparse.Namespace) -> int:
    st = _style(args)
    tape = _load(args.tape)
    exs = tapeops.filter_exchanges(tape, include=args.include or ())
    if args.json:
        print(json.dumps([{"id": e.id, "request": e.label, "status": e.response.status, "duration_ms": e.duration_ms,
                           "recorded_at": e.recorded_at} for e in exs], indent=2))  # fmt: skip
        return EXIT_OK
    for e in exs:
        print(f"  {st(e.id[:10], 'muted')}  {st(f'{e.request.method:<7}', e.request.method)}"
              f"{st.status(e.response.status)}  {st(f'{format_ms(e.duration_ms):>8}', 'muted')}  "
              f"{e.request.target}{st('  templated', 'accent') if e.templated else ''}")  # fmt: skip
    print(st(f"  {len(exs)} of {len(tape)} exchanges in {tape.path}", "muted"))
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    st = _style(args)
    ex = _load(args.tape).get(args.id)
    if args.json:
        print(json.dumps(ex.to_dict(), indent=2, ensure_ascii=False))
        return EXIT_OK
    print(st.rule(f"{ex.request.method} {ex.request.target}"))
    print(
        f"  {st('id', 'muted')} {ex.id}   {st('recorded', 'muted')} {ex.recorded_at}   "
        f"{st('upstream', 'muted')} {ex.upstream or '-'}   {st('took', 'muted')} {format_ms(ex.duration_ms)}"
    )
    for title, headers, body, ctype in (
        ("request", ex.request.headers, ex.request.body, ex.request.content_type),
        (f"response {ex.response.status} {ex.response.reason}", ex.response.headers, ex.response.body,
         ex.response.content_type),
    ):  # fmt: skip
        print()
        print(st(f"  {title}", "accent_strong"))
        for k, v in headers:
            print(f"    {st(k, 'muted')}: {v}")
        if body.encoding == "json":
            text = json.dumps(body.content, indent=2, ensure_ascii=False)
            print("\n".join("    " + line for line in text.splitlines()))
        elif body.encoding == "text":
            print("\n".join("    " + line for line in str(body.content).splitlines()[:200]))
        elif body.encoding == "base64":
            print(f"    <{format_size(len(body.to_bytes(ctype)))} binary>")
    return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    st = _style(args)
    tape = _load(args.tape)
    stats = tapeops.endpoint_stats(tape)
    if args.json:
        print(json.dumps([s.to_dict() for s in stats], indent=2))
        return EXIT_OK
    print(st.rule(f"{tape.header.name}: {len(tape)} exchanges, {len(stats)} endpoints"))
    print(
        st(f"  {'METHOD':<7} {'ENDPOINT':<44} {'COUNT':>5}  {'P50':>8} {'P95':>8}  {'AVG SIZE':>9}  STATUSES", "muted")
    )
    for s in stats:
        statuses = " ".join(f"{st.status(code)}×{n}" for code, n in s.statuses.items())
        print(f"  {st(f'{s.method:<7}', s.method)} {s.path:<44} {s.count:>5}  {format_ms(s.p50_ms):>8} "
              f"{format_ms(s.p95_ms):>8}  {format_size(s.avg_size):>9}  {statuses}")  # fmt: skip
    return EXIT_OK


# ------------------------------------------------------------------- compare
def _render_comparison(cmp: Comparison, fmt: str, st: Style) -> str:
    if fmt == "json":
        return json.dumps(cmp.to_dict(), indent=2, ensure_ascii=False) + "\n"
    lines: list[str] = []
    if fmt == "markdown":
        counts = f"**{len(cmp.changed)}** changed, {len(cmp.only_old)} missing, {len(cmp.only_new)} new."
        lines += [f"# Reelay: {cmp.title}", "", f"Compared **{len(cmp.pairs)}** exchanges, {counts}", ""]
        for p in cmp.changed:
            lines.append(f"### `{p.label}`")
            if p.old_status != p.new_status:
                lines.append(f"- status {p.old_status} → {p.new_status}")
            lines += [f"- {n}" for n in p.notes]
            lines += [f"- `{d.describe()}`" for d in p.deltas]
            lines.append("")
        lines += [f"- missing: `{label}`" for label in cmp.only_old]
        lines += [f"- new: `{label}`" for label in cmp.only_new]
        return "\n".join(lines) + "\n"
    lines.append(st.rule(cmp.title))
    for p in cmp.changed:
        status = (f"{st.status(p.old_status or 0)} {st.arrow} {st.status(p.new_status or 0)}"
                  if p.old_status != p.new_status else st.status(p.new_status or 0))  # fmt: skip
        lines.append(f"  {st('changed', 'warn')}  {p.label}  {status}")
        lines += [f"           {st(st.dot, 'accent')} {n}" for n in p.notes]
        for d in p.deltas[:40]:
            role = {"added": "ok", "removed": "err", "type": "warn"}.get(d.op, "accent")
            lines.append(f"           {st(d.describe(), role)}")
        if len(p.deltas) > 40:
            lines.append(st(f"           … {len(p.deltas) - 40} more", "muted"))
    lines += [f"  {st('missing', 'err')}  {label}" for label in cmp.only_old]
    lines += [f"  {st('new', 'ok')}      {label}" for label in cmp.only_new]
    summary = f"{len(cmp.changed)} changed, {len(cmp.only_old)} missing, {len(cmp.only_new)} new"
    verdict = st(f"{st.ok_mark} no differences", "ok") if not cmp.has_differences else st(summary, "warn")
    lines += ["", f"  compared {len(cmp.pairs)} exchange(s): {verdict}", ""]
    return "\n".join(lines) + "\n"


def cmd_diff(args: argparse.Namespace) -> int:
    s = _settings(args)
    old, new = _load(args.old), _load(args.new)
    cmp = compare_tapes(old, new, ignore=[*s.diff_ignore, *(args.ignore or [])], settings=s.match,
                        title=f"{Path(args.old).name} {_style(args).arrow} {Path(args.new).name}")  # fmt: skip
    _write(_render_comparison(cmp, args.format, _style(args)), args.output, "diff")
    return EXIT_DIFFERENCES if cmp.has_differences else EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    s = _settings(args)
    tape = _load(args.tape)
    target_url = args.target or s.server.target or tape.header.upstream
    if not target_url:
        raise UsageError("no target to verify against", hint="pass --target https://api.example.com")
    client = UpstreamClient(Target.parse(target_url), timeout=s.server.timeout,
                            verify_tls=not args.insecure and s.server.verify_tls)  # fmt: skip

    def send(ex: Exchange) -> Response:
        return client.send(ex.request)[0]

    methods = [m.strip().upper() for m in args.methods.split(",") if m.strip()]
    cmp = verify_live(tape, send, ignore=[*s.diff_ignore, *(args.ignore or [])], methods=methods,
                      title=f"{tape.path.name if tape.path else 'tape'} vs {target_url}")  # fmt: skip
    _write(_render_comparison(cmp, args.format, _style(args)), args.output, "verification report")
    return EXIT_DIFFERENCES if cmp.has_differences else EXIT_OK


# ------------------------------------------------------------ transformation
def cmd_infer(args: argparse.Namespace) -> int:
    tape = _load(args.tape)
    doc = openapi.build(tape, title=args.title or f"{tape.header.name} API", server=tape.header.upstream,
                        examples=not args.no_examples)  # fmt: skip
    fmt = args.format or ("json" if args.output and args.output.endswith(".json") else "yaml")
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n" if fmt == "json" else yamlout.dump(doc)
    _write(text, args.output, f"OpenAPI 3.1 document ({len(doc['paths'])} paths)")
    return EXIT_OK


def cmd_har_import(args: argparse.Namespace) -> int:
    try:
        data = json.loads(Path(args.har).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(f"file not found: {args.har}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot read HAR {args.har}: {exc}") from None
    exchanges, upstream = from_har(data)
    redactor = Redactor(_settings(args).redact)
    tape = Tape.new(Path(args.output), upstream=upstream)
    tape.replace_all(redactor.exchange(e) for e in exchanges)
    tape.save()
    print(f"imported {len(exchanges)} exchanges into {args.output} (secrets redacted)", file=sys.stderr)
    return EXIT_OK


def cmd_har_export(args: argparse.Namespace) -> int:
    tape = _load(args.tape)
    _write(json.dumps(to_har(tape.exchanges, tape.header.upstream), indent=2, ensure_ascii=False) + "\n",
           args.output, f"HAR with {len(tape)} entries")  # fmt: skip
    return EXIT_OK


def cmd_filter(args: argparse.Namespace) -> int:
    tape = _load(args.tape)
    kept = tapeops.filter_exchanges(tape, include=args.include or (), exclude=args.exclude or (),
                                    status=args.status or ())  # fmt: skip
    out = Tape(Path(args.output), tape.header, kept)
    out.save()
    print(f"kept {len(kept)} of {len(tape)} exchanges in {args.output}", file=sys.stderr)
    return EXIT_OK


def cmd_merge(args: argparse.Namespace) -> int:
    tapes = [_load(p) for p in args.tapes]
    merged = tapeops.merge(tapes, dedupe=args.dedupe, settings=_settings(args).match)
    out = Tape.new(Path(args.output), upstream=tapes[0].header.upstream)
    out.replace_all(merged)
    out.save()
    print(f"merged {sum(len(t) for t in tapes)} exchanges into {len(merged)} in {args.output}", file=sys.stderr)
    return EXIT_OK


def cmd_redact(args: argparse.Namespace) -> int:
    tape = _load(args.tape)
    cleaned, changed = tapeops.redact_all(tape, Redactor(_settings(args).redact))
    target = Path(args.output) if args.output else tape.path
    assert target is not None
    if not args.output and changed and not args.no_backup:
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_bytes(target.read_bytes())
        print(f"backup written to {backup}", file=sys.stderr)
    tape.replace_all(cleaned)
    tape.save(target)
    print(f"redacted {changed} of {len(cleaned)} exchanges, saved {target}", file=sys.stderr)
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path or CONFIG_FILENAME)
    if path.exists() and not args.force:
        raise UsageError(f"{path} already exists", hint="pass --force to overwrite it")
    path.write_text(STARTER, encoding="utf-8")
    print(f"wrote {path}. Edit [server] target, then run: reelay serve")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    st = _style(args)
    s = _settings(args)
    srv = s.server
    print(f"{st(st.ok_mark, 'ok')} {s.source or 'no config file, using defaults'}")
    rows: list[tuple[str, Any]] = [
        ("mode", srv.mode), ("listen", f"{srv.host}:{srv.port}"), ("target", srv.target or "-"),
        ("tape", srv.tape or "-"), ("on miss", srv.on_miss), ("match on", ", ".join(s.match.on)),
        ("strategy", s.match.strategy), ("redaction", "on" if s.redact.enabled else "OFF"),
        ("chaos rules", f"{len(s.chaos.rules)} ({'enabled' if s.chaos.enabled else 'disabled'})"),
        ("diff ignore", ", ".join(s.diff_ignore) or "-"),
    ]  # fmt: skip
    for key, value in rows:
        print(f"  {st(key.ljust(12), 'muted')} {value}")
    return EXIT_OK


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="PATH", help="reelay.toml to use")
    common.add_argument("-v", "--verbose", action="count", default=0, help="more logging (-vv for debug)")
    common.add_argument("-q", "--quiet", action="store_true", help="only errors")
    common.add_argument("--no-color", action="store_true", help="disable colours")

    parser = argparse.ArgumentParser(prog="reelay", description="Record real HTTP API traffic. Replay it anywhere.",
                                     epilog=f"Created by {__author__}. Docs: {__url__}")  # fmt: skip
    parser.add_argument("-V", "--version", action="version", version=f"reelay {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, handler: Handler, text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=text, description=text)
        p.set_defaults(handler=handler)
        return p

    def server_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("-t", "--tape", help="tape file")
        p.add_argument("--target", help="upstream base URL, e.g. https://api.example.com")
        p.add_argument("--host", help="listen address (default 127.0.0.1)")
        p.add_argument("-p", "--port", type=int, help="listen port (default 8080, 0 = any free port)")
        p.add_argument("--on-miss", choices=ON_MISS, help="replay: what to do when nothing matches")
        p.add_argument("--strategy", choices=STRATEGIES, help="how repeated identical requests are served")
        p.add_argument("--fuzzy", action="store_true", help="serve the closest recording when nothing matches")
        p.add_argument("--insecure", action="store_true", help="do not verify upstream TLS certificates")
        p.add_argument("--no-dashboard", action="store_true", help="disable the live dashboard")
        p.add_argument("--cors", action="store_true", help="add permissive CORS headers (browser front-ends)")
        p.add_argument("--chaos", action=argparse.BooleanOptionalAction, default=None, help="enable chaos rules")
        p.add_argument("--seed", type=int, help="random seed for chaos and templates")

    def fmt_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("-f", "--format", choices=("text", "json", "markdown"), default="text")
        p.add_argument("-o", "--output", help="write the report to a file")
        p.add_argument("--ignore", action="append", metavar="JSONPATH", help="ignore a JSON location (repeatable)")

    server_opts(add("record", cmd_record, "proxy to an upstream API and record every exchange"))
    server_opts(add("replay", cmd_replay, "serve recorded exchanges as a mock API"))
    p = add("serve", cmd_serve, "run with the mode from reelay.toml (or --mode)")
    server_opts(p)
    p.add_argument("-m", "--mode", choices=MODES, help="record, replay, hybrid or passthrough")

    p = add("ls", cmd_ls, "list the exchanges on a tape")
    p.add_argument("tape")
    p.add_argument("-i", "--include", action="append", metavar="ROUTE", help="only routes like 'GET /users/*'")
    p.add_argument("--json", action="store_true")

    p = add("show", cmd_show, "show one exchange in full")
    p.add_argument("tape")
    p.add_argument("id", help="exchange id or unique prefix")
    p.add_argument("--json", action="store_true")

    p = add("stats", cmd_stats, "per-endpoint statistics for a tape")
    p.add_argument("tape")
    p.add_argument("--json", action="store_true")

    p = add("diff", cmd_diff, "compare responses between two tapes (API regression check)")
    p.add_argument("old")
    p.add_argument("new")
    fmt_opts(p)

    p = add("verify", cmd_verify, "re-send recorded requests to the live API and diff the answers")
    p.add_argument("tape")
    p.add_argument("--target", help="API base URL (default: the tape's upstream)")
    p.add_argument("--methods", default="GET,HEAD,OPTIONS", help="methods to re-send (default: safe methods only)")
    p.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    fmt_opts(p)

    p = add("infer", cmd_infer, "generate an OpenAPI 3.1 spec from a tape")
    p.add_argument("tape")
    p.add_argument("-o", "--output", help="output file (.yaml or .json)")
    p.add_argument("-f", "--format", choices=("yaml", "json"))
    p.add_argument("--title", help="API title")
    p.add_argument("--no-examples", action="store_true", help="leave recorded examples out of the spec")

    p = add("har-import", cmd_har_import, "convert a HAR file into a tape (secrets redacted)")
    p.add_argument("har")
    p.add_argument("-o", "--output", required=True, help="tape to write")

    p = add("har-export", cmd_har_export, "convert a tape into a HAR file")
    p.add_argument("tape")
    p.add_argument("-o", "--output", help="HAR file to write (default: stdout)")

    p = add("filter", cmd_filter, "copy matching exchanges into a new tape")
    p.add_argument("tape")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("-i", "--include", action="append", metavar="ROUTE")
    p.add_argument("-x", "--exclude", action="append", metavar="ROUTE")
    p.add_argument("-s", "--status", action="append", type=int, metavar="CODE")

    p = add("merge", cmd_merge, "combine several tapes")
    p.add_argument("tapes", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--dedupe", action="store_true", help="keep only the newest exchange per request")

    p = add("redact", cmd_redact, "re-apply redaction rules to an existing tape")
    p.add_argument("tape")
    p.add_argument("-o", "--output", help="write here instead of in place")
    p.add_argument("--no-backup", action="store_true", help="do not keep a .bak copy when editing in place")

    p = add("init", cmd_init, "write a commented reelay.toml")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")

    add("config", cmd_config, "validate the configuration and print a summary")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_USAGE
    _setup_logging(-1 if args.quiet else args.verbose)
    try:
        handler: Handler = args.handler
        return handler(args)
    except ReelayError as exc:
        log.error("%s", exc.message)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return EXIT_OK
    except Exception:  # pragma: no cover
        log.exception("unexpected error, please report it at %s/issues", __url__)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
