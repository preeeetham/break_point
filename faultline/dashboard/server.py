"""Faultline live dashboard: a standard-library HTTP server over the ReportStore blackboard.

Run with ``python -m faultline.dashboard.server [--port N]``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ..config import load_settings
from ..reporting.store import ReportStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------- helpers
def _safe(fn, default=None):
    """Call ``fn`` and swallow any failure (missing/corrupt files) -> default."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - dashboard must never crash on bad reports
        return default


def _dump(model) -> Optional[dict]:
    if model is None:
        return None
    return model.model_dump(mode="json")


def _pick(d: Optional[dict], *keys: str) -> Optional[dict]:
    if d is None:
        return None
    return {k: d.get(k) for k in keys}


# ---------------------------------------------------------------- state builders
def _topology_state(store: ReportStore) -> Optional[dict]:
    topo = _dump(_safe(store.topology))
    if not topo:
        return None
    services = {
        name: _pick(svc, "role", "stateful", "entrypoint", "container", "state", "health")
        for name, svc in (topo.get("services") or {}).items()
    }
    criticality = [
        _pick(c, "service", "score", "fan_in", "transitive_dependents", "reasons")
        for c in topo.get("criticality") or []
    ]
    edges = [_pick(e, "src", "dst", "kind") for e in topo.get("edges") or []]
    return {"services": services, "criticality": criticality, "edges": edges}


def _baseline_state(store: ReportStore) -> Optional[dict]:
    b = _dump(_safe(store.baseline))
    if not b:
        return None
    return {
        "captured_at": b.get("captured_at"),
        "api": _pick(b.get("api"), "success_rate", "p50_ms", "p95_ms", "p99_ms", "sample_count"),
        "thresholds": b.get("thresholds") or {},
    }


def _experiment_summary(store: ReportStore, r: dict) -> dict:
    eid = r.get("experiment_id")
    risk = _dump(_safe(lambda: store.risk(eid)))
    fix = _dump(_safe(lambda: store.fix(eid)))
    baseline = r.get("baseline") or {}
    during = r.get("during") or {}
    after = r.get("after") or {}
    recovery = r.get("recovery") or {}
    safety = r.get("safety") or {}
    hyp_result = r.get("hypothesis_result") or {}
    return {
        "experiment_id": eid,
        "status": r.get("status"),
        "started_at": r.get("started_at"),
        "ended_at": r.get("ended_at"),
        "target_service": (r.get("target") or {}).get("service"),
        "failure_type": r.get("failure_type"),
        "parameters": r.get("parameters") or {},
        "rationale": r.get("rationale"),
        "planned_by": r.get("planned_by"),
        "hypothesis": _pick(
            r.get("hypothesis"), "statement", "expected_affected", "expected_max_error_rate", "expected_recovery_s"
        ),
        "hypothesis_held": hyp_result.get("held"),
        "baseline_success_rate": baseline.get("success_rate"),
        "during_success_rate": during.get("success_rate"),
        "during_p95_ms": during.get("p95_ms"),
        "after_success_rate": after.get("success_rate"),
        "time_to_recover_s": recovery.get("time_to_recover_s"),
        "outage_duration_s": recovery.get("outage_duration_s"),
        "recovered": recovery.get("recovered"),
        "aborted_early": safety.get("aborted_early"),
        "abort_reason": safety.get("abort_reason"),
        "affected_services_observed": r.get("affected_services_observed") or [],
        "findings": [_pick(f, "code", "severity", "title", "evidence") for f in r.get("findings") or []],
        "risk": _pick(risk, "severity", "level", "summary", "production_implication"),
        "fix": (
            {
                "recommendations": [
                    _pick(rec, "title", "category", "effort", "confidence", "rationale", "steps", "verify")
                    for rec in fix.get("recommendations") or []
                ]
            }
            if fix
            else None
        ),
        "timeline": [_pick(t, "t_plus_s", "event", "detail") for t in r.get("timeline") or []],
    }


def _experiments_state(store: ReportStore) -> list[dict]:
    reports = _safe(store.experiments, default=[]) or []
    out = []
    for rep in reports:
        d = _safe(lambda: _dump(rep))
        if d:
            out.append(_experiment_summary(store, d))
    out.sort(key=lambda e: e.get("started_at") or "", reverse=True)
    return out


def _scorecard_state(store: ReportStore) -> Optional[dict]:
    s = _dump(_safe(store.scorecard))
    if not s:
        return None
    return {
        "overall_score": s.get("overall_score"),
        "summary": s.get("summary"),
        "ranking": [
            _pick(r, "service", "score", "grade", "experiments", "worst_success_rate", "slowest_recovery_s", "findings")
            for r in s.get("ranking") or []
        ],
    }


def build_state(store: ReportStore) -> dict:
    """Snapshot of everything the dashboard needs, as a JSON-serialisable dict."""
    return {
        "now": time.time(),
        "stop_requested": bool(_safe(store.stop_requested, default=False)),
        "active": _safe(store.active),
        "live": _safe(store.live),
        "guardian": _safe(lambda: store.heartbeat("guardian")),
        "topology": _topology_state(store),
        "baseline": _baseline_state(store),
        "experiments": _experiments_state(store),
        "scorecard": _scorecard_state(store),
    }


def experiment_detail(store: ReportStore, eid: str) -> Optional[dict]:
    report = _dump(_safe(lambda: store.experiment(eid)))
    if report is None:
        return None
    return {
        "experiment": report,
        "risk": _dump(_safe(lambda: store.risk(eid))),
        "fix": _dump(_safe(lambda: store.fix(eid))),
    }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    store: ReportStore  # injected via make_handler
    server_version = "Faultline/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature fixed by base class
        pass

    # ---- response helpers
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _not_found(self) -> None:
        self._json(404, {"error": "not found", "path": self.path})

    # ---- routes
    def do_GET(self) -> None:  # noqa: N802 - name fixed by base class
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            index = STATIC_DIR / "index.html"
            if not index.is_file():
                self._send(500, b"static/index.html missing", "text/plain; charset=utf-8")
                return
            self._send(200, index.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._json(200, build_state(self.store))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
        elif path.startswith("/api/experiment/"):
            eid = path[len("/api/experiment/"):].strip("/")
            if not eid or "/" in eid or ".." in eid:
                self._not_found()
                return
            detail = experiment_detail(self.store, eid)
            if detail is None:
                self._not_found()
            else:
                self._json(200, detail)
        else:
            self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        # drain any body so the connection can be reused
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path == "/api/stop":
            self.store.request_stop("dashboard")
            self._json(200, {"ok": True, "stop_requested": True})
        elif path == "/api/resume":
            self.store.clear_stop()
            self._json(200, {"ok": True, "stop_requested": False})
        else:
            self._not_found()


def make_handler(store: ReportStore) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"store": store})


def serve(store: ReportStore, port: int, host: str = "127.0.0.1") -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    httpd.daemon_threads = True
    print(f"Faultline dashboard -> http://{host}:{port}/  (reports: {store.root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.", flush=True)
    finally:
        httpd.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(prog="faultline-dashboard", description="Faultline live dashboard")
    parser.add_argument("--port", type=int, default=settings.dashboard_port)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reports", default=str(settings.reports), help="reports directory (blackboard)")
    args = parser.parse_args(argv)
    serve(ReportStore(args.reports), port=args.port, host=args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
