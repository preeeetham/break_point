"""HTTP adapter over :class:`faultline.dashboard.control.ControlPlane`.

Standard library only. Run with ``python -m faultline.dashboard.server [--port N]`` or
``faultline dashboard``.
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..config import load_settings
from ..reporting.store import ReportStore
from .control import JOB_KINDS, ControlPlane, JobBusy

STATIC_DIR = Path(__file__).resolve().parent / "static"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
        ".woff2": "font/woff2", ".png": "image/png", ".ico": "image/x-icon",
        ".map": "application/json; charset=utf-8"}
POST_JOBS = {f"/api/jobs/{k}" for k in JOB_KINDS if k != "plan"}
MAX_BODY = 1 << 20


class BadRequest(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    control: ControlPlane  # injected by make_handler
    server_version = "Faultline/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            # the unread body would be parsed as the next request on a kept-alive socket
            self.close_connection = True
            if length < 0:
                raise BadRequest("invalid Content-Length")
            raise BadRequest(f"body exceeds {MAX_BODY} bytes", 413)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BadRequest(f"invalid JSON body: {exc}") from None
        if not isinstance(data, dict):
            raise BadRequest("JSON body must be an object")
        return data

    def _static(self, name: str) -> None:
        path = (STATIC_DIR / name).resolve()
        if not path.is_relative_to(STATIC_DIR.resolve()) or not path.is_file():
            self._json(404, {"error": "not found", "path": name})
            return
        self._send(200, path.read_bytes(), MIME.get(path.suffix, "application/octet-stream"))

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path == "/api/state":
                try:
                    since = int((query.get("since") or ["0"])[0] or 0)
                except ValueError:
                    self._json(400, {"error": "since must be an integer"})
                    return
                self._json(200, self.control.state(since))
            elif path == "/api/approvals":
                # Read-only on purpose, and there is deliberately no approve/reject POST and no job
                # kind: the answer comes back by email from the admin's own mailbox. A button here
                # would let whoever already has the dashboard open wave through the very experiment
                # the gate exists to hold back, which is the thing being demonstrated.
                self._json(200, self.control.approvals())
            elif path == "/api/catalog":
                self._json(200, self.control.catalog())
            elif path == "/api/plan":
                use_llm = (query.get("llm") or ["0"])[0] in ("1", "true", "yes")
                protected = (query.get("allow_protected") or ["0"])[0] in ("1", "true", "yes")
                self._json(200, self.control.plan(use_llm, protected))
            elif path.startswith("/api/experiment/"):
                eid = path[len("/api/experiment/"):].strip("/")
                if not eid or "/" in eid or ".." in eid:
                    self._json(404, {"error": "not found"})
                    return
                detail = self.control.experiment_detail(eid)
                self._json(200, detail) if detail else self._json(404, {"error": "no such experiment"})
            else:
                self._json(404, {"error": "not found", "path": path})
        except Exception as exc:  # the dashboard must never die on a bad request
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        c = self.control
        try:
            body = self._body()
            if path == "/api/stop":
                self._json(200, c.stop())
            elif path == "/api/resume":
                self._json(200, c.resume())
            elif path == "/api/arm":
                self._json(200, {**c.arm(), "session": c.state(c.events.seq)["session"]})
            elif path == "/api/disarm":
                self._json(200, c.disarm())
            elif path == "/api/jobs/cancel":
                self._json(200, c.cancel())
            elif path in POST_JOBS:
                kind = path.rsplit("/", 1)[-1]
                self._json(202, {"ok": True, "job": c.submit(kind, body).as_dict()})
            else:
                self._json(404, {"error": "not found", "path": path})
        except BadRequest as exc:
            self._json(exc.status, {"error": str(exc)})
        except JobBusy as exc:
            self._json(409, {"error": str(exc), "job": c.job.as_dict() if c.job else None})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def make_handler(control: ControlPlane) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"control": control})


def serve(store: ReportStore, port: int, host: str = "127.0.0.1", settings=None) -> None:
    settings = settings or load_settings()
    control = ControlPlane(settings, store)
    httpd = ThreadingHTTPServer((host, port), make_handler(control))
    httpd.daemon_threads = True
    print(f"\n  Faultline control room -> http://{host}:{port}/", flush=True)
    print(f"  project {settings.compose_project} · reports {store.root}\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.", flush=True)
    finally:
        control.shutdown()
        httpd.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    settings = load_settings()
    p = argparse.ArgumentParser(prog="faultline-dashboard", description="Faultline control room")
    p.add_argument("--port", type=int, default=settings.dashboard_port)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--reports", default=str(settings.reports))
    a = p.parse_args(argv)
    serve(ReportStore(a.reports), port=a.port, host=a.host, settings=settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
