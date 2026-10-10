"""The stdlib HTTP adapter, served for real on an ephemeral port against a control plane whose docker,
probes and LLM are mocked out (see test_control)."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import faultline.dashboard.server as server_mod
from faultline.dashboard.control import ControlPlane
from faultline.dashboard.server import make_handler

from .test_control import STATE_KEYS, block_handler, make_session_cls, wait_job  # noqa: F401
from .test_control import sessions, settings  # noqa: F401  (fixtures)


@pytest.fixture
def control(settings, tmp_store, sessions):  # noqa: F811
    c = ControlPlane(settings, tmp_store)
    yield c
    c.shutdown()


@pytest.fixture
def base(control):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(control))
    httpd.daemon_threads = True
    # the raw-socket tests drop connections mid-request; keep handler tracebacks out of the run
    httpd.handle_error = lambda request, client_address: None
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base: str, method: str, path: str, body: bytes | dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    if method == "POST" and data is None:
        data = b""
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    if data is not None and "Content-Type" not in (headers or {}):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw, status, hdrs = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, e.headers
    ctype = hdrs.get("Content-Type", "")
    return status, (json.loads(raw) if ctype.startswith("application/json") else raw), hdrs


def raw_request(base: str, request: bytes, timeout: float = 1.5) -> bytes:
    host, port = base.rsplit("/", 1)[-1].split(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.sendall(request)
        try:
            return s.recv(65536)
        except socket.timeout:
            return b""


# ---------------------------------------------------------------- state + events
def test_state_endpoint_shape_and_headers(base):
    status, st, hdrs = call(base, "GET", "/api/state")
    assert status == 200 and STATE_KEYS <= set(st)
    assert hdrs["Cache-Control"] == "no-store"
    assert hdrs["Content-Type"].startswith("application/json")
    assert st["session"]["armed"] is False and st["job"] is None


def test_state_since_only_returns_newer_events(base):
    seq = call(base, "GET", "/api/state")[1]["seq"]
    call(base, "POST", "/api/stop", {})
    call(base, "POST", "/api/resume", {})
    st = call(base, "GET", f"/api/state?since={seq}")[1]
    assert [e["seq"] for e in st["events"]] == [seq + 1, seq + 2]
    assert "KILL SWITCH" in st["events"][0]["text"] and st["seq"] == seq + 2
    assert call(base, "GET", f"/api/state?since={st['seq']}")[1]["events"] == []
    assert call(base, "GET", "/api/state?since=")[1]["events"][0]["seq"] == 1


# ---------------------------------------------------------------- jobs
def test_second_job_is_refused_with_409(base, control, monkeypatch):
    release = block_handler(control, "_job_topology", monkeypatch)
    status, out, _ = call(base, "POST", "/api/jobs/topology", {})
    try:
        assert status == 202 and out["ok"] is True
        assert out["job"]["kind"] == "topology" and out["job"]["status"] == "running"
        for path, body in (("/api/jobs/baseline", {"duration": 30}), ("/api/jobs/run", {"mode": "auto"}),
                           ("/api/jobs/analyze", {}), ("/api/jobs/heal", {}), ("/api/jobs/topology", {})):
            status, busy, _ = call(base, "POST", path, body)
            assert status == 409, path
            assert "already running" in busy["error"] and busy["job"]["id"] == out["job"]["id"]
        assert call(base, "GET", "/api/state")[1]["job"]["id"] == out["job"]["id"]
    finally:
        release.set()
    wait_job(control)
    monkeypatch.setattr(control, "_job_analyze", lambda body: {})
    status, again, _ = call(base, "POST", "/api/jobs/analyze", {"llm": False})
    assert status == 202 and again["job"]["id"] != out["job"]["id"]
    wait_job(control)


def test_cancel_without_a_job_is_a_no_op(base):
    status, out, _ = call(base, "POST", "/api/jobs/cancel", {})
    assert status == 200 and out == {"ok": True, "cancelled": False}


# ---------------------------------------------------------------- kill switch
def test_stop_and_resume_over_http(base, control, tmp_store, monkeypatch):
    release = block_handler(control, "_job_baseline", monkeypatch)
    assert call(base, "POST", "/api/jobs/baseline", {"duration": 20})[0] == 202
    try:
        status, out, _ = call(base, "POST", "/api/stop", {})  # never queued behind the running job
        assert status == 200 and out == {"ok": True, "stop_requested": True}
        assert tmp_store.stop_file.exists()
        assert call(base, "GET", "/api/state")[1]["stop_requested"] is True
    finally:
        release.set()
    wait_job(control)
    status, out, _ = call(base, "POST", "/api/resume")
    assert status == 200 and out == {"ok": True, "stop_requested": False}
    assert not tmp_store.stop_file.exists()
    assert call(base, "GET", "/api/state")[1]["stop_requested"] is False


# ---------------------------------------------------------------- read-only previews + session
def test_plan_is_read_only_over_http(base, tmp_store, topology, sessions):  # noqa: F811
    status, out, _ = call(base, "GET", "/api/plan?llm=0")
    assert status >= 400 and "error" in out  # no map yet
    tmp_store.write_topology(topology)
    status, out, _ = call(base, "GET", "/api/plan?llm=1&allow_protected=0")
    assert status == 200 and set(out) == {"candidates", "pick", "llm"}
    assert out["pick"]["planned_by"] == "deterministic"
    assert sessions == []
    st = call(base, "GET", "/api/state")[1]
    assert st["session"]["armed"] is False and st["telemetry"]["points"] == []


def test_catalog_over_http(base, tmp_store, topology, sessions):  # noqa: F811
    tmp_store.write_topology(topology)
    status, out, _ = call(base, "GET", "/api/catalog")
    assert status == 200 and len(out["failures"]) == 10
    assert {"key", "params", "applies_to", "policy_blocked_on", "available"} <= set(out["failures"][0])
    assert sessions == []


def test_arm_and_disarm_over_http(base, sessions):  # noqa: F811
    status, out, _ = call(base, "POST", "/api/arm", {})
    assert status == 200 and out["armed"] is True and out["session"]["armed"] is True
    assert out["session"]["probe"]["running"] is True and len(sessions) == 1
    status, out, _ = call(base, "POST", "/api/disarm", {})
    assert status == 200 and out == {"armed": False} and sessions[0].closed


def test_experiment_detail_over_http(base, tmp_store, make_report):
    tmp_store.write_experiment(make_report(experiment_id="abc123"))
    status, out, _ = call(base, "GET", "/api/experiment/abc123")
    assert status == 200 and out["experiment"]["experiment_id"] == "abc123" and out["markdown"]
    assert call(base, "GET", "/api/experiment/nope")[0] == 404


# ---------------------------------------------------------------- routing + errors
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/nope"),
    ("GET", "/api/jobs/run"),
    ("GET", "/api/experiment/"),
    ("GET", "/static/missing.js"),
    ("POST", "/api/nope"),
    ("POST", "/api/jobs/plan"),  # plan is GET-only and never runs anything
    ("POST", "/api/jobs/explode"),
    ("POST", "/api/state"),
])
def test_unknown_routes_404_json(base, control, method, path):
    status, out, hdrs = call(base, method, path, {} if method == "POST" else None)
    assert status == 404 and isinstance(out, dict) and "error" in out
    assert hdrs["Content-Type"].startswith("application/json")
    assert control.job is None


def test_static_files_and_traversal(base):
    status, body, hdrs = call(base, "GET", "/")
    assert status == 200 and hdrs["Content-Type"].startswith("text/html") and b"<" in body
    status, _, hdrs = call(base, "GET", "/static/app.js")
    assert status == 200 and hdrs["Content-Type"].startswith("text/javascript")
    status, _, hdrs = call(base, "GET", "/static/tokens.css")
    assert status == 200 and hdrs["Content-Type"].startswith("text/css")
    conn = http.client.HTTPConnection(base.split("//")[1], timeout=5)
    conn.request("GET", "/static/../server.py")  # http.client sends the path verbatim
    r = conn.getresponse()
    assert r.status == 404 and b"import" not in r.read()
    conn.close()


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"run"'])
def test_bad_json_body_is_400(base, control, monkeypatch, raw):
    monkeypatch.setattr(control, "_job_topology", lambda body: {})
    status, out, _ = call(base, "POST", "/api/jobs/topology", raw)
    if control.job is not None:
        wait_job(control)
    assert status == 400 and "error" in out
    assert control.job is None


def test_bad_since_param_is_400(base):
    status, out, _ = call(base, "GET", "/api/state?since=abc")
    assert status == 400 and "error" in out


def test_non_numeric_content_length_gets_a_response(base):
    resp = raw_request(base, b"POST /api/resume HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n"
                             b"Connection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 400")


def test_negative_content_length_does_not_hang(base):
    resp = raw_request(base, b"POST /api/nope HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n", timeout=1.0)
    assert resp.startswith(b"HTTP/1.1 4")


def test_static_guard_rejects_sibling_prefix_dirs(base, tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    secret = tmp_path / "static_private"
    secret.mkdir()
    (secret / "secret.txt").write_text("top secret")
    monkeypatch.setattr(server_mod, "STATIC_DIR", static)
    conn = http.client.HTTPConnection(base.split("//")[1], timeout=5)
    conn.request("GET", "/static/../static_private/secret.txt")
    r = conn.getresponse()
    body = r.read()
    conn.close()
    assert r.status == 404 and b"top secret" not in body
