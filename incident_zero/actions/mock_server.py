"""A stand-in Action API, run as its own process: `incident_zero action-api`.

It owns every write to the system, so "Action API only" is structural: the agent process holds an HTTP client, not
a Docker client it could write with. Each execution is recorded with what it changed, so reversible actions can be
rolled back. Its own kill switch refuses every execution until resumed. One action at a time per service.

The runtime is pluggable: DockerRuntime acts on the Compose project, tests use a fake.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .catalog import MOCK_ACTIONS, ActionDef, validate


class RuntimeErrorForAction(RuntimeError):
    pass


class DockerRuntime:
    """Acts on the Compose project through Break Point's DockerController."""

    def __init__(self, project: str, network: str, demo_dir: str = "", tc_image: str = ""):
        from break_point.chaos.docker_ctl import DockerController

        self.d = DockerController(project, network)
        self.demo_dir = demo_dir
        self.tc_image = tc_image or "ghcr.io/alexei-led/pumba-alpine-nettools:latest"

    def services(self) -> set[str]:
        return set(self.d.containers())

    def describe(self, service: str) -> str:
        st = self.d.state(service)
        return f"{service} is {st.get('status')} (restarts={st.get('restart_count', st.get('restarts', '?'))})"

    def restart_service(self, service: str, dry: bool) -> tuple[str, Optional[dict]]:
        if dry:
            return f"would restart {service}; now: {self.describe(service)}", None
        self.d.restart(service)
        ok = self.d.wait_running(service, 60)
        return f"restarted {service}; running={ok}", None

    def start_service(self, service: str, dry: bool) -> tuple[str, Optional[dict]]:
        c = self.d.container(service)
        if dry:
            return f"would start/unpause {service}; now: {c.status}", None
        if c.status == "paused":
            self.d.unpause(service)
        elif c.status != "running":
            self.d.start(service)
        ok = self.d.wait_running(service, 60)
        return f"started {service}; running={ok}", None

    def update_resources(self, service: str, dry: bool, cpus: Optional[float] = None,
                         memory_mb: Optional[int] = None) -> tuple[str, Optional[dict]]:
        c = self.d.container(service)
        hc = c.attrs.get("HostConfig") or {}
        prev = {"nano_cpus": hc.get("NanoCpus") or 0, "memory": hc.get("Memory") or 0,
                "memswap": hc.get("MemorySwap") or 0}
        kw: dict[str, Any] = {}
        if cpus:
            kw["nano_cpus"] = int(cpus * 1e9)
        if memory_mb:
            kw["mem_limit"] = int(memory_mb) * 2**20
            kw["memswap_limit"] = int(memory_mb) * 2**20 * 2
        if dry:
            return (f"would set {service} limits {kw} (now cpus={prev['nano_cpus'] / 1e9 or 'unlimited'}, "
                    f"memory_mb={prev['memory'] // 2**20 or 'unlimited'})"), None
        c.update(**kw)
        return f"updated {service} limits: {kw}", {"kind": "resources", "service": service, "prev": prev}

    def clear_network_impairment(self, service: str, dry: bool) -> tuple[str, Optional[dict]]:
        if dry:
            return f"would remove any tc netem qdisc from {service} eth0", None
        from break_point.chaos.actions import clear_qdisc

        ok = clear_qdisc(self.d, self.tc_image, self.d.container(service).name)
        return f"cleared netem on {service}: {'removed' if ok else 'nothing to remove'}", None

    def reconnect_network(self, service: str, dry: bool) -> tuple[str, Optional[dict]]:
        connected = self.d.is_connected(service)
        if dry:
            return f"would reconnect {service} to {self.d.network_name} (connected now: {connected})", None
        if not connected:
            self.d.connect(service, [service])
        return f"{service} connected to {self.d.network_name}", None

    def rollback_release(self, service: str, dry: bool) -> tuple[str, Optional[dict]]:
        from .flags import FlagFile

        if not self.demo_dir:
            raise RuntimeErrorForAction("rollback_release needs demo_dir (the demo checkout) in the mock's settings")
        ff = FlagFile(self.demo_dir)
        faulty = ff.faulty(service)
        if dry:
            return (f"would roll {service} back to its last good release" +
                    (" (a faulty change is live)" if faulty else " (no faulty change found)")), None
        if not faulty:
            return f"{service} is already on its last good release; nothing rolled back", None
        prev = ff.set({f: ff.off_variant(f) for f in faulty})
        return f"rolled {service} back to its last good release", {"kind": "flags", "prev": prev}

    def undo(self, undo: dict) -> str:
        if undo["kind"] == "resources":
            p = undo["prev"]
            c = self.d.container(undo["service"])
            c.update(nano_cpus=p["nano_cpus"], mem_limit=p["memory"] or -1, memswap_limit=p["memswap"] or -1)
            return f"restored {undo['service']} limits"
        if undo["kind"] == "flags":
            from .flags import FlagFile

            FlagFile(self.demo_dir).set(undo["prev"])
            return "restored the previous release"
        raise RuntimeErrorForAction(f"cannot undo {undo['kind']}")


class ActionAPI:
    def __init__(self, runtime: Any, state_file: Optional[Path] = None, catalog: list[ActionDef] = MOCK_ACTIONS):
        self.runtime, self.catalog = runtime, catalog
        self.state_file = state_file
        self.killed = False
        self.executions: dict[str, dict] = {}
        self._svc_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        if state_file and state_file.is_file():
            data = json.loads(state_file.read_text())
            self.killed = bool(data.get("killed"))
            self.executions = data.get("executions", {})

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"killed": self.killed, "executions": self.executions}, indent=2))

    def execute(self, body: dict) -> tuple[int, dict]:
        action, params = body.get("action", ""), body.get("parameters") or {}
        dry = bool(body.get("dry_run", True))
        if self.killed and not dry:
            return 423, {"ok": False, "message": "kill switch engaged: no actions until /resume"}
        try:
            services = self.runtime.services()
        except Exception as exc:
            return 503, {"ok": False, "message": f"runtime unavailable: {exc}"}
        errs = validate(action, params, self.catalog, services)
        if errs:
            return 400, {"ok": False, "message": "; ".join(errs)}
        if not body.get("reason"):
            return 400, {"ok": False, "message": "a reason is required"}
        svc = params["service"]
        lock = self._svc_locks[svc]
        if not lock.acquire(timeout=0.1):
            return 409, {"ok": False, "message": f"another action is running on {svc}"}
        try:
            fn = getattr(self.runtime, action)
            extra = {k: v for k, v in params.items() if k != "service"}
            message, undo = fn(svc, dry, **extra)
        except Exception as exc:
            return 500, {"ok": False, "message": f"{action} failed: {exc}"}
        finally:
            lock.release()
        ex_id = f"X-{uuid.uuid4().hex[:10]}"
        self.executions[ex_id] = {"id": ex_id, "action": action, "parameters": params, "dry_run": dry,
                                  "reason": body.get("reason"), "incident_id": body.get("incident_id", ""),
                                  "at": time.time(), "message": message, "undo": undo, "rolled_back": False}
        self._save()
        return 200, {"ok": True, "execution_id": ex_id, "message": message, "dry_run": dry,
                     "reversible": undo is not None}

    def rollback(self, body: dict) -> tuple[int, dict]:
        ex = self.executions.get(body.get("execution_id", ""))
        if ex is None:
            return 404, {"ok": False, "message": "unknown execution"}
        if ex["dry_run"]:
            return 200, {"ok": True, "message": "dry run: nothing to roll back"}
        if ex["rolled_back"]:
            return 200, {"ok": True, "message": "already rolled back"}
        if not ex.get("undo"):
            return 409, {"ok": False, "message": f"{ex['action']} is not reversible"}
        try:
            msg = self.runtime.undo(ex["undo"])
        except Exception as exc:
            return 500, {"ok": False, "message": f"rollback failed: {exc}"}
        ex["rolled_back"] = True
        self._save()
        return 200, {"ok": True, "message": msg}


def make_handler(api: ActionAPI, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorised(self) -> bool:
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {"ok": False, "message": "unauthorised"})
                return False
            return True

        def do_GET(self):
            if not self._authorised():
                return
            if self.path == "/health":
                return self._send(200, {"ok": True, "killed": api.killed})
            if self.path == "/actions":
                return self._send(200, {"actions": [a.model_dump() for a in api.catalog]})
            if self.path == "/executions":
                return self._send(200, {"executions": list(api.executions.values())})
            self._send(404, {"ok": False, "message": "not found"})

        def do_POST(self):
            if not self._authorised():
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "message": "bad json"})
            if self.path == "/actions/execute":
                return self._send(*api.execute(body))
            if self.path == "/actions/rollback":
                return self._send(*api.rollback(body))
            if self.path == "/kill":
                api.killed = True
                api._save()
                return self._send(200, {"ok": True, "killed": True})
            if self.path == "/resume":
                api.killed = False
                api._save()
                return self._send(200, {"ok": True, "killed": False})
            self._send(404, {"ok": False, "message": "not found"})

    return Handler


def serve(api: ActionAPI, host: str, port: int, token: str = "") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(api, token))
    srv.daemon_threads = True
    return srv
