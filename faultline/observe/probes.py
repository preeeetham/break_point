"""Two independent signal sources that run for the whole session:

ThingsApiProbe  synthetic user traffic through nginx -> gateway (what a real client sees), plus a
                data-integrity canary: every write carries a sequence number that is read back later.
HealthProbe     what the platform *claims*: /alive and /ready on each Ditto service (port 7626, via
                docker exec because the port is not published), mongosh ping for MongoDB, and the Docker
                health status. Comparing the two is how we catch health checks that lie (Ditto #1865).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from ..schemas import HealthSummary, Topology
from .window import Sample, SampleWindow

Log = Callable[[str], None]


class ThingsApiProbe(threading.Thread):
    OPS = ("get_thing", "put_property", "get_thing", "search", "get_property", "put_property", "get_thing", "search")

    def __init__(self, base_url: str, user: str, password: str, window: SampleWindow, rps: float = 4.0,
                 namespace: str = "faultline.probe", timeout_s: float = 5.0, log: Log | None = None):
        super().__init__(daemon=True, name="api-probe")
        self.base = base_url.rstrip("/") + "/api/2"
        self.auth = (user, password)
        self.window = window
        self.rps = rps
        self.timeout = timeout_s
        self.canary = f"{namespace}:canary"
        self.log = log or (lambda m: None)
        self.seq = 0
        self.last_acked_seq: Optional[int] = None
        self.ready = False
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")
        self._inflight = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ setup
    def ensure_canary(self, attempts: int = 5) -> bool:
        body = {"attributes": {"kind": "faultline-canary"},
                "features": {"telemetry": {"properties": {"seq": 0, "temperature": 20.0}}}}
        for _ in range(attempts):
            try:
                r = requests.put(f"{self.base}/things/{self.canary}", json=body, auth=self.auth, timeout=self.timeout)
                if r.status_code in (200, 201, 204):
                    self.ready = True
                    self.last_acked_seq = 0
                    return True
                self.log(f"canary create returned HTTP {r.status_code}: {r.text[:120]}")
            except requests.RequestException as exc:
                self.log(f"canary create failed: {type(exc).__name__}")
            time.sleep(2)
        return False

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        if not self.ready and not self.ensure_canary():
            self.log("api probe: canary could not be created, probing anyway")
        interval = 1.0 / self.rps
        i = 0
        nxt = time.time()
        while not self._stop.is_set():
            op = self.OPS[i % len(self.OPS)]
            i += 1
            with self._lock:
                busy = self._inflight
            if busy < 16:
                self._pool.submit(self._do, op)
            nxt += interval
            delay = nxt - time.time()
            if delay > 0:
                self._stop.wait(delay)
            else:
                nxt = time.time()

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _do(self, op: str) -> None:
        with self._lock:
            self._inflight += 1
        started = time.time()
        status, err, seq = 0, "", None
        try:
            if op == "get_thing":
                r = requests.get(f"{self.base}/things/{self.canary}", auth=self.auth, timeout=self.timeout)
            elif op == "get_property":
                r = requests.get(f"{self.base}/things/{self.canary}/features/telemetry/properties/seq",
                                 auth=self.auth, timeout=self.timeout)
            elif op == "search":
                r = requests.get(f"{self.base}/search/things", auth=self.auth, timeout=self.timeout,
                                 params={"filter": 'eq(attributes/kind,"faultline-canary")', "option": "size(1)"})
            else:
                with self._lock:
                    self.seq += 1
                    seq = self.seq
                r = requests.put(f"{self.base}/things/{self.canary}/features/telemetry/properties/seq",
                                 json=seq, auth=self.auth, timeout=self.timeout)
            status = r.status_code
            if op == "put_property" and 200 <= status < 300 and seq is not None:
                with self._lock:
                    self.last_acked_seq = max(self.last_acked_seq or 0, seq)
        except requests.Timeout:
            err = "timeout"
        except requests.ConnectionError:
            err = "connection_error"
        except requests.RequestException as exc:
            err = type(exc).__name__
        finally:
            with self._lock:
                self._inflight -= 1
        self.window.add(Sample(started, op, status, (time.time() - started) * 1000, err))

    # ------------------------------------------------------------------ durability canary
    def integrity_check(self) -> tuple[Optional[bool], str]:
        try:
            r = requests.get(f"{self.base}/things/{self.canary}/features/telemetry/properties/seq",
                             auth=self.auth, timeout=self.timeout)
        except requests.RequestException as exc:
            return None, f"read-back failed: {type(exc).__name__}"
        if r.status_code != 200:
            return None, f"read-back failed: HTTP {r.status_code}"
        try:
            stored = int(r.json())
        except (ValueError, TypeError):
            return None, f"read-back returned non-integer: {r.text[:40]}"
        acked = self.last_acked_seq or 0
        ok = stored >= acked
        return ok, f"stored seq={stored}, last acknowledged write seq={acked}" + ("" if ok else " -> ACKNOWLEDGED WRITE LOST")


# ---------------------------------------------------------------------- health
@dataclass(frozen=True)
class HealthSample:
    ts: float
    service: str
    status: Optional[str]      # docker: running / exited / paused / missing
    docker_health: Optional[str]
    alive: Optional[int]       # HTTP code of /alive (or mongo ping mapped to 200/503), None = not applicable/unreachable
    ready: Optional[int]

    @property
    def healthy(self) -> bool:
        if self.status != "running":
            return False
        if self.alive is None:
            return True  # no application endpoint: running is the best signal we have
        return self.alive == 200


class HealthProbe(threading.Thread):
    def __init__(self, docker, topology: Topology, interval_s: float = 3.0, log: Log | None = None):
        super().__init__(daemon=True, name="health-probe")
        self.docker = docker
        self.topology = topology
        self.interval = interval_s
        self.log = log or (lambda m: None)
        self.services = topology.relevant_services()
        self._samples: deque[HealthSample] = deque(maxlen=20000)
        self._latest: dict[str, HealthSample] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="health")

    def _check(self, svc: str) -> HealthSample:
        node = self.topology.services[svc]
        st = self.docker.state(svc)
        status = st.get("status")
        if status != "running" or st.get("paused"):
            return HealthSample(time.time(), svc, "paused" if st.get("paused") else status, st.get("health"), None, None)
        alive = ready = None
        try:
            if node.health_port:
                p = node.health_port
                code, out = self.docker.shell(
                    svc, f"curl -s -o /dev/null -m 2 -w '%{{http_code}}' localhost:{p}/alive; printf ' '; "
                         f"curl -s -o /dev/null -m 2 -w '%{{http_code}}' localhost:{p}/ready")
                parts = out.strip().split()
                if len(parts) == 2:
                    alive, ready = int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 0
            elif node.role == "datastore" and "mongo" in node.image:
                code, out = self.docker.shell(svc, "timeout 3 mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null")
                alive = 200 if out.strip().endswith("1") else 503
        except Exception as exc:  # docker exec on a dying container
            self.log(f"health {svc}: {type(exc).__name__}")
            alive = 0 if node.health_port else None
        return HealthSample(time.time(), svc, status, st.get("health"), alive, ready)

    def run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            results = list(self._pool.map(self._check, self.services))
            with self._lock:
                for s in results:
                    self._samples.append(s)
                    self._latest[s.service] = s
            self._stop.wait(max(0.5, self.interval - (time.time() - t0)))

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def latest(self) -> dict[str, HealthSample]:
        with self._lock:
            return dict(self._latest)

    def between(self, t0: float, t1: float | None = None) -> list[HealthSample]:
        t1 = t1 if t1 is not None else time.time()
        with self._lock:
            return [s for s in self._samples if t0 <= s.ts <= t1]

    def consecutive_failures(self, svc: str) -> int:
        with self._lock:
            items = [s for s in self._samples if s.service == svc]
        n = 0
        for s in reversed(items):
            if s.healthy:
                break
            n += 1
        return n

    def all_healthy(self, services: list[str]) -> bool:
        latest = self.latest()
        return all(svc in latest and latest[svc].healthy for svc in services)

    def availability(self, t0: float, t1: float | None = None) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        for svc in self.services:
            items = [s for s in self.between(t0, t1) if s.service == svc]
            out[svc] = round(sum(1 for s in items if s.healthy) / len(items), 4) if items else None
        return out


def summarize_health(samples: list[HealthSample], services: list[str]) -> list[HealthSummary]:
    out = []
    for svc in services:
        items = [s for s in samples if s.service == svc]
        if not items:
            continue
        alive = [s.alive for s in items if s.alive is not None]
        ready = [s.ready for s in items if s.ready is not None]
        out.append(HealthSummary(
            service=svc, ticks=len(items),
            alive_ok_fraction=round(sum(1 for a in alive if a == 200) / len(alive), 3) if alive else None,
            ready_ok_fraction=round(sum(1 for r in ready if r == 200) / len(ready), 3) if ready else None,
            docker_unhealthy_seen=any(s.docker_health == "unhealthy" for s in items),
            container_down_seen=any(s.status != "running" for s in items),
        ))
    return out
