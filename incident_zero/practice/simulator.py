"""The Simulator: injects a scenario's faults into the running demo, and reverts them.

Application faults switch the demo's flags (actions.flags). Infrastructure faults go through Break Point's
DockerController: stop, pause, a kill loop, CPU and memory limits, netem delay/loss via a Pumba helper, network
disconnect. Each injection returns an undo record; `revert` applies them in reverse order and is idempotent.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from ..actions.flags import FlagFile

SCENARIO_FILE = Path(__file__).with_name("scenarios.yaml")
PUMBA_IMAGE = "gaiaadm/pumba:latest"
TC_IMAGE = "ghcr.io/alexei-led/pumba-alpine-nettools:latest"


def load_scenarios(path: Path = SCENARIO_FILE) -> list[dict]:
    return (yaml.safe_load(path.read_text()) or {}).get("scenarios", [])


class Simulator:
    def __init__(self, settings, docker: Any = None):
        self.settings = settings
        self._docker = docker
        self._loops: dict[str, threading.Event] = {}

    @property
    def docker(self):
        if self._docker is None:
            from break_point.chaos.docker_ctl import DockerController

            self._docker = DockerController(self.settings.compose_project, "opentelemetry-demo")
        return self._docker

    def inject(self, run_id: str, scenario: dict, duration_s: float) -> list[dict]:
        undo: list[dict] = []
        try:
            for f in scenario["faults"]:
                undo.append(self._apply(run_id, f, duration_s))
        except Exception:
            self.revert(undo)
            raise
        return undo

    def _apply(self, run_id: str, f: dict, duration_s: float) -> dict:
        kind, svc = f["kind"], f.get("service")
        if kind == "flag":
            prev = FlagFile(self.settings.demo_dir).set({f["flag"]: str(f["variant"])})
            return {"kind": "flag", "prev": prev}
        d = self.docker
        if kind == "stop":
            d.stop(svc)
            return {"kind": "start", "service": svc}
        if kind == "pause":
            d.pause(svc)
            return {"kind": "unpause", "service": svc}
        if kind == "kill_loop":
            stop = threading.Event()
            self._loops[run_id + svc] = stop
            interval = float(f.get("interval_s", 20))

            def loop():
                while not stop.is_set():
                    try:
                        d.kill(svc)
                        time.sleep(2)
                        d.start(svc)
                    except Exception:
                        pass
                    stop.wait(interval)
            threading.Thread(target=loop, daemon=True, name=f"kill-loop-{svc}").start()
            return {"kind": "stop_loop", "key": run_id + svc, "service": svc}
        if kind in ("cpu", "memory"):
            c = d.container(svc)
            hc = c.attrs.get("HostConfig") or {}
            prev = {"nano_cpus": hc.get("NanoCpus") or 0, "memory": hc.get("Memory") or 0,
                    "memswap": hc.get("MemorySwap") or 0}
            if kind == "cpu":
                c.update(nano_cpus=int(float(f["cpus"]) * 1e9))
            else:
                m = int(f["memory_mb"]) * 2**20
                c.update(mem_limit=m, memswap_limit=m)
            return {"kind": "limits", "service": svc, "prev": prev}
        if kind in ("delay", "loss"):
            for img in (PUMBA_IMAGE, TC_IMAGE):
                if not d.has_image(img):
                    d.pull(img)
            netem = (["delay", "--time", str(int(f["delay_ms"])), "--jitter", str(int(f.get("jitter_ms", 0)))]
                     if kind == "delay" else ["loss", "--percent", str(int(f["percent"]))])
            cmd = ["--log-level", "info", "netem", "--duration", f"{int(duration_s)}s", "--tc-image", TC_IMAGE,
                   "--interface", "eth0", *netem, d.container(svc).name]
            helper = d.run_helper(PUMBA_IMAGE, cmd, name=f"iz-pumba-{run_id[-8:]}-{svc}",
                                  volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}})
            return {"kind": "netem", "service": svc, "helper": helper.id}
        if kind == "disconnect":
            aliases = d.network_aliases(svc)
            d.disconnect(svc)
            return {"kind": "connect", "service": svc, "aliases": aliases}
        raise ValueError(f"unknown fault kind {kind}")

    def revert(self, undo: list[dict]) -> list[str]:
        notes = []
        for u in reversed(undo):
            try:
                notes.append(self._undo(u))
            except Exception as exc:
                notes.append(f"revert {u['kind']} failed: {exc}")
        return notes

    def _undo(self, u: dict) -> str:
        k = u["kind"]
        if k == "flag":
            FlagFile(self.settings.demo_dir).set(u["prev"])
            return f"flags restored {u['prev']}"
        d = self.docker
        if k == "start":
            c = d.container(u["service"])
            if c.status != "running":
                d.start(u["service"])
            return f"started {u['service']}"
        if k == "unpause":
            if d.container(u["service"]).status == "paused":
                d.unpause(u["service"])
            return f"unpaused {u['service']}"
        if k == "stop_loop":
            ev = self._loops.pop(u["key"], None)
            if ev:
                ev.set()
            if d.container(u["service"]).status != "running":
                d.start(u["service"])
            return f"kill loop on {u['service']} stopped"
        if k == "limits":
            p = u["prev"]
            d.container(u["service"]).update(nano_cpus=p["nano_cpus"], mem_limit=p["memory"] or -1,
                                             memswap_limit=p["memswap"] or -1)
            return f"limits of {u['service']} restored"
        if k == "netem":
            try:
                d.remove_helper(d.client.containers.get(u["helper"]))
            except Exception:
                pass
            from break_point.chaos.actions import clear_qdisc

            clear_qdisc(d, TC_IMAGE, d.container(u["service"]).name)
            return f"netem on {u['service']} removed"
        if k == "connect":
            if not d.is_connected(u["service"]):
                d.connect(u["service"], u["aliases"])
            return f"{u['service']} reconnected"
        return f"nothing to undo for {k}"


def scenario(scenarios: list[dict], scenario_id: str) -> Optional[dict]:
    return next((s for s in scenarios if s["id"] == scenario_id), None)
