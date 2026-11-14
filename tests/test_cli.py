"""CLI commands, driven through main()."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reelay import __version__
from reelay.cli import main
from reelay.tape import Tape

from .conftest import ex, jget


@pytest.fixture
def tape(tmp_path: Path) -> Path:
    path = tmp_path / "api.tape"
    t = Tape.new(path, upstream="https://api.example.com")
    t.replace_all([
        ex(path="/users/1", resp={"id": 1, "name": "a", "updated_at": "1"}, id="01HAAAAAAAAAAAAAAAAAAAAAA1"),
        ex(path="/users/2", resp={"id": 2, "name": "b"}, id="01HAAAAAAAAAAAAAAAAAAAAAA2"),
        ex("POST", "/users", body={"name": "c"}, status=201, resp={"id": 3}, id="01HAAAAAAAAAAAAAAAAAAAAAA3"),
        ex(path="/health", resp={"ok": True}, id="01HAAAAAAAAAAAAAAAAAAAAAA4",
           headers=[("Authorization", "Bearer leakedtoken123")]),
    ])  # fmt: skip
    t.save()
    return path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_version_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out
    assert main([]) == 2


def test_ls_show_stats(capsys: pytest.CaptureFixture[str], tape: Path) -> None:
    code, out, _ = run(capsys, "ls", str(tape), "--no-color")
    assert code == 0 and "/users/1" in out and "4 of 4" in out
    code, out, _ = run(capsys, "ls", str(tape), "--include", "GET /users/*", "--json")
    assert len(json.loads(out)) == 2
    code, out, _ = run(capsys, "show", str(tape), "01HAAAAAAAAAAAAAAAAAAAAAA3", "--no-color")
    assert code == 0 and "POST /users" in out and '"name": "c"' in out
    code, out, _ = run(capsys, "show", str(tape), "01HAAAAAAAAAAAAAAAAAAAAAA3", "--json")
    assert json.loads(out)["response"]["status"] == 201
    code, out, _ = run(capsys, "stats", str(tape), "--json")
    paths = {(s["method"], s["path"]) for s in json.loads(out)}
    assert ("GET", "/users/{userId}") in paths
    code, out, _ = run(capsys, "stats", str(tape), "--no-color")
    assert "/users/{userId}" in out
    assert run(capsys, "show", str(tape), "zzz")[0] == 2


def test_diff_exit_codes_and_formats(capsys: pytest.CaptureFixture[str], tape: Path, tmp_path: Path) -> None:
    other = tmp_path / "v2.tape"
    t = Tape.load(tape)
    changed = [e.with_(response=e.response.__class__(e.response.status, e.response.headers,
                                                     e.response.body.__class__("json", {**e.response.body.content, "name": "renamed", "updated_at": "2"}),
                                                     e.response.reason)) if e.request.path == "/users/1" else e
               for e in t]  # fmt: skip
    Tape(other, t.header, changed).save()
    assert run(capsys, "diff", str(tape), str(tape))[0] == 0
    code, out, _ = run(capsys, "diff", str(tape), str(other), "--no-color")
    assert code == 1 and "$.name" in out and "$.updated_at" in out
    code, out, _ = run(capsys, "diff", str(tape), str(other), "--ignore", "$..updated_at", "-f", "json")
    data = json.loads(out)
    assert [d["path"] for d in data["changed"][0]["deltas"]] == ["$.name"]
    code, out, _ = run(capsys, "diff", str(tape), str(other), "-f", "markdown")
    assert out.startswith("# Reelay:") and "`~ $.name" in out


def test_infer_yaml_and_json(capsys: pytest.CaptureFixture[str], tape: Path, tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    out_yaml = tmp_path / "openapi.yaml"
    assert run(capsys, "infer", str(tape), "-o", str(out_yaml), "--title", "Demo")[0] == 0
    doc = yaml.safe_load(out_yaml.read_text())
    assert doc["info"]["title"] == "Demo" and "/users/{userId}" in doc["paths"]
    _, out, _ = run(capsys, "infer", str(tape), "-f", "json", "--no-examples")
    assert '"example"' not in out and json.loads(out)["openapi"] == "3.1.0"


def test_har_roundtrip_filter_merge_redact(capsys: pytest.CaptureFixture[str], tape: Path, tmp_path: Path) -> None:
    har = tmp_path / "x.har"
    assert run(capsys, "har-export", str(tape), "-o", str(har))[0] == 0
    imported = tmp_path / "imported.tape"
    assert run(capsys, "har-import", str(har), "-o", str(imported))[0] == 0
    assert len(Tape.load(imported)) == 4
    assert "leakedtoken" not in imported.read_text()  # import redacts

    filtered = tmp_path / "f.tape"
    assert run(capsys, "filter", str(tape), "-o", str(filtered), "-i", "GET /users/*")[0] == 0
    assert len(Tape.load(filtered)) == 2
    merged = tmp_path / "m.tape"
    assert run(capsys, "merge", str(tape), str(filtered), "-o", str(merged), "--dedupe")[0] == 0
    assert len(Tape.load(merged)) == 4

    assert "leakedtoken" in tape.read_text()
    code, _, err = run(capsys, "redact", str(tape))
    assert code == 0 and "redacted 1 of 4" in err
    assert "leakedtoken" not in tape.read_text()
    assert "leakedtoken" in tape.with_suffix(".tape.bak").read_text()
    assert run(capsys, "har-import", str(tmp_path / "missing.har"), "-o", str(merged))[0] == 2


def test_verify_against_live_api(capsys: pytest.CaptureFixture[str], upstream: str, tmp_path: Path) -> None:
    path = tmp_path / "live.tape"
    t = Tape.new(path, upstream=upstream)
    t.replace_all([ex(path="/users/5", resp={"id": 5, "name": "user 5", "email": "u5@example.com",
                                             "updated_at": "2026-01-01T00:00:00Z"}),
                   ex(path="/users/6", resp={"id": 6, "name": "old name", "email": "u6@example.com",
                                             "updated_at": "2026-01-01T00:00:00Z"}),
                   ex("DELETE", "/users/6")])  # fmt: skip
    t.save()
    code, out, _ = run(capsys, "verify", str(path), "--no-color")
    assert code == 1 and "$.name" in out and "/users/6" in out and "DELETE" not in out
    code, out, _ = run(capsys, "verify", str(path), "-f", "json")
    assert len(json.loads(out)["changed"]) == 1


def test_init_and_config(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = tmp_path / "reelay.toml"
    assert run(capsys, "init", str(cfg))[0] == 0
    assert run(capsys, "init", str(cfg))[0] == 2
    code, out, _ = run(capsys, "config", "-c", str(cfg), "--no-color")
    assert code == 0 and "hybrid" in out and "sequential" in out
    bad = tmp_path / "bad.toml"
    bad.write_text("[server]\nmode = 'live'\n")
    assert run(capsys, "config", "-c", str(bad))[0] == 2


def test_missing_tape_is_usage_error(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = run(capsys, "ls", str(tmp_path / "none.tape"))
    assert code == 2 and "reelay record" in err


def test_record_and_replay_as_real_processes(upstream: str, tmp_path: Path) -> None:
    """Run the actual CLI in a subprocess, record through it, stop it, then replay."""
    tape = tmp_path / "proc.tape"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"), "NO_COLOR": "1"}

    def start(*args: str) -> tuple[subprocess.Popen[str], str]:
        proc = subprocess.Popen([sys.executable, "-m", "reelay", *args, "--port", "0"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)  # fmt: skip
        assert proc.stdout is not None
        deadline = time.time() + 15
        while time.time() < deadline:
            line = proc.stdout.readline()
            if "listening" in line:
                return proc, line.split()[-1]
        proc.kill()
        raise AssertionError("server did not start")

    proc, url = start("record", "--target", upstream, "--tape", str(tape))
    try:
        assert jget(url + "/users/11")[2]["id"] == 11
    finally:
        proc.terminate()
        proc.wait(10)
    assert len(Tape.load(tape)) == 1
    proc, url = start("replay", "--tape", str(tape), "--no-dashboard")
    try:
        status, headers, body = jget(url + "/users/11")
        assert status == 200 and headers["x-reelay"] == "replayed" and body["id"] == 11
    finally:
        proc.terminate()
        proc.wait(10)
