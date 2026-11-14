#!/usr/bin/env python3
"""Generate README visuals from a real Reelay session.

Starts a small sample API (clearly fake data), records traffic through
Reelay in hybrid mode with chaos rules, then captures:

* docs/assets/session.svg    live request log printed by ``reelay serve``
* docs/assets/diff.svg       ``reelay diff`` between two API versions
* docs/assets/dashboard.png  the live dashboard (needs Google Chrome)

Usage: python scripts/make_demo.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from reelay.cli import main  # noqa: E402
from reelay.config import Settings  # noqa: E402
from reelay.server import ReelayServer  # noqa: E402
from reelay.tape import Tape  # noqa: E402

ASSETS = ROOT / "docs" / "assets"
SGR = re.compile(r"\x1b\[([0-9;]*)m")
CHAR_W, LINE_H, PAD = 8.43, 20, 22
DEFAULT_FG = "#d4d4d8"


def ansi_to_svg(text: str, title: str) -> str:
    lines = text.rstrip("\n").split("\n")
    cols = max(len(SGR.sub("", line)) for line in lines)
    width = int(cols * CHAR_W + PAD * 2)
    height = int(len(lines) * LINE_H + PAD * 2 + 34)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        "<style>text{font:13.5px 'JetBrains Mono','Cascadia Code',Menlo,Consolas,monospace;white-space:pre}</style>",
        f'<rect width="{width}" height="{height}" rx="12" fill="#0b0b0f" stroke="#26262d"/>',
        f'<rect width="{width}" height="34" rx="12" fill="#141419"/>',
        f'<rect y="22" width="{width}" height="12" fill="#141419"/>',
        '<circle cx="20" cy="17" r="5.5" fill="#3f3f46"/><circle cx="38" cy="17" r="5.5" fill="#3f3f46"/>'
        '<circle cx="56" cy="17" r="5.5" fill="#3f3f46"/>',
        f'<text x="{width / 2}" y="21.5" fill="#71717a" text-anchor="middle">{escape(title)}</text>',
    ]
    y = 34 + PAD + 13
    for line in lines:
        col, fg, bg, bold = 0, DEFAULT_FG, None, False
        spans: list[str] = []
        pos = 0
        for match in [*SGR.finditer(line), None]:
            end = match.start() if match else len(line)
            chunk = line[pos:end]
            if chunk:
                x = PAD + col * CHAR_W
                if bg:
                    out.append(f'<rect x="{x - 1:.1f}" y="{y - 14}" width="{len(chunk) * CHAR_W + 2:.1f}" '
                               f'height="19" rx="3" fill="{bg}"/>')  # fmt: skip
                weight = ' font-weight="700"' if bold else ""
                spans.append(f'<tspan x="{x:.1f}" fill="{fg}"{weight}>{escape(chunk)}</tspan>')
                col += len(chunk)
            if match is None:
                break
            pos = match.end()
            codes = [int(c) for c in match.group(1).split(";") if c] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    fg, bg, bold = DEFAULT_FG, None, False
                elif c == 1:
                    bold = True
                elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                    color = "#{:02x}{:02x}{:02x}".format(*codes[i + 2 : i + 5])
                    if c == 38:
                        fg = color
                    else:
                        bg = color
                    i += 4
                i += 1
        if spans:
            out.append(f'<text y="{y}" xml:space="preserve">{"".join(spans)}</text>')
        y += LINE_H
    out.append("</svg>")
    return "\n".join(out) + "\n"


PLANETS = {  # sample data for the demo API
    "mercury": {"moons": 0, "day_hours": 4222.6},
    "venus": {"moons": 0, "day_hours": 2802.0},
    "earth": {"moons": 1, "day_hours": 24.0},
    "mars": {"moons": 2, "day_hours": 24.7},
    "jupiter": {"moons": 95, "day_hours": 9.9},
}


def sample_api(version: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_GET(self) -> None:
            path = self.path.split("?")[0].strip("/").split("/")
            time.sleep(0.02)
            if path == ["planets"]:
                body = {"planets": sorted(PLANETS), "count": len(PLANETS)}
            elif len(path) == 2 and path[0] == "planets" and path[1] in PLANETS:
                body = {"name": path[1], **PLANETS[path[1]], "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
                if version == 2:
                    body["moons"] = {"count": body.pop("moons")}
                    body["mass_kg"] = None
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "21")
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def get(url: str, headers: dict[str, str] | None = None) -> None:
    with contextlib.suppress(Exception):
        urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10).read()


def main_demo() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "FORCE_COLOR": "1", "COLUMNS": "96"}
    with tempfile.TemporaryDirectory(prefix="reelay-demo-") as tmp:
        base = Path(tmp)
        v1, v2 = sample_api(1), sample_api(2)
        up1 = f"http://127.0.0.1:{v1.server_address[1]}"
        up2 = f"http://127.0.0.1:{v2.server_address[1]}"
        cfg = base / "reelay.toml"
        cfg.write_text("""
[chaos]
seed = 3
[[chaos.rule]]
route = "GET /planets/jupiter"
latency = "300ms..500ms"
[[chaos.rule]]
route = "GET /planets/venus"
error_rate = 1.0
error_status = 503
""")
        tape1 = base / "planets.tape"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "reelay",
                "serve",
                "-c",
                str(cfg),
                "--mode",
                "hybrid",
                "--target",
                up1,
                "--tape",
                str(tape1),
                "--port",
                "0",
                "--chaos",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert proc.stdout is not None
        banner: list[str] = []
        url = ""
        while True:
            line = proc.stdout.readline()
            banner.append(line)
            plain = SGR.sub("", line)
            if "listening" in plain:
                url = plain.split()[-1]
            if "Ctrl+C" in plain:
                banner.append(proc.stdout.readline())
                break
        traffic = [
            "/planets",
            "/planets/earth",
            "/planets/mars",
            "/planets/earth",
            "/planets/jupiter",
            "/planets/venus",
            "/planets/pluto",
            "/planets/mars?api_key=sk_live_demo",
            "/planets",
        ]
        for path in traffic:
            get(url + path, {"Authorization": "Bearer demo-token-123456"})
            time.sleep(0.05)
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if chrome:
            subprocess.run(
                [
                    chrome,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--force-device-scale-factor=2",
                    "--window-size=1360,860",
                    "--timeout=9000",
                    f"--screenshot={ASSETS / 'dashboard.png'}",
                    f"{url}/__reelay/",
                ],
                capture_output=True,
                timeout=90,
                check=False,
            )
        proc.terminate()
        rest, _ = proc.communicate(timeout=10)
        lines = [line for line in (banner + rest.splitlines(keepends=True)) if line.strip()]
        session = "".join(lines)

        # record the same calls against v2 of the API and diff the tapes
        tape2 = base / "planets-v2.tape"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            s = Settings()
            s.server.port, s.server.mode, s.server.target = 0, "record", up2
            with ReelayServer(s, Tape.open_or_create(tape2, up2)) as srv:
                for path in ("/planets", "/planets/earth", "/planets/mars"):
                    get(srv.url + path)
        buffer = io.StringIO()
        os.environ["FORCE_COLOR"] = "1"
        with contextlib.redirect_stdout(buffer):
            main(["diff", str(tape1), str(tape2), "--ignore", "$..updated_at"])
        diff_out = buffer.getvalue()

    clean = lambda text: re.sub(r"/tmp/reelay-demo-[^/\s]+/", "", text)  # noqa: E731
    (ASSETS / "session.svg").write_text(ansi_to_svg(clean(session), "reelay serve --mode hybrid"), encoding="utf-8")
    (ASSETS / "diff.svg").write_text(
        ansi_to_svg(clean(diff_out), "reelay diff planets.tape planets-v2.tape"), encoding="utf-8"
    )
    print(f"wrote demo assets to {ASSETS}")


if __name__ == "__main__":
    main_demo()
