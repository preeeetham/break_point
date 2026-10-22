"""Two independent signal sources that run for the whole session:

HttpApiProbe  synthetic user traffic through the target's entry point (what a real client sees), driven by
              the target's probe spec (targets/<name>/probe.yaml): which requests to send and in what mix,
              which services each one passes through, and optionally a data-integrity canary -- every write
              carries a sequence number that is read back later. Without a spec it polls the health URL.
HealthProbe   what the platform *claims*: each service's own liveness and readiness endpoints (via docker
              exec, because the port is usually not published), the engine's own ping for a datastore whose
              engine is known, and the Docker health status. Comparing the two is how we catch health
              checks that lie: a service answering "ready" while every request through it fails.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import requests
import yaml

from ..schemas import HealthSummary, Topology
from ..topology import datastores
from .window import Sample, SampleWindow

Log = Callable[[str], None]

SEQ = "{seq}"              # a request body that is exactly this string is replaced by the write's sequence number
NAMESPACE = "{namespace}"  # replaced in paths, params and bodies by settings.probe_namespace
DEFAULT_HEALTH_PATH = "/"


# ---------------------------------------------------------------------- probe spec
@dataclass(frozen=True)
class ProbeRequest:
    """One synthetic request. `route` names the services it passes through, entry first: cascade
    attribution uses it to say whose traffic failed while their own health checks stayed green.
    Services absent from the topology are dropped at use."""
    name: str
    method: str = "GET"
    path: str = DEFAULT_HEALTH_PATH
    json: Any = None
    params: dict = field(default_factory=dict)
    route: tuple[str, ...] = ()
    write: bool = False    # carries the sequence number; a 2xx acknowledges it for the integrity check


@dataclass(frozen=True)
class ProbeSpec:
    health_path: str = DEFAULT_HEALTH_PATH
    health_expect: dict = field(default_factory=dict)   # key/values the health response's JSON must contain
    setup: tuple[ProbeRequest, ...] = ()                # sent once before probing, e.g. to create a canary
    operations: dict[str, ProbeRequest] = field(default_factory=dict)
    mix: tuple[str, ...] = ()
    integrity: str = ""    # an operation whose JSON answer is the last stored sequence number
    spike: str = ""        # the operation traffic_spike repeats

    @property
    def routes(self) -> dict[str, list[str]]:
        return {name: list(op.route) for name, op in self.operations.items()}


def _request(name: str, raw: dict) -> ProbeRequest:
    if not isinstance(raw, dict):
        raise ValueError(f"probe operation {name!r} must be a mapping, got {type(raw).__name__}")
    return ProbeRequest(name=name, method=str(raw.get("method", "GET")).upper(), path=str(raw.get("path", "/")),
                        json=raw.get("json"), params=dict(raw.get("params") or {}),
                        route=tuple(str(s) for s in raw.get("route") or ()), write=bool(raw.get("write", False)))


def health_only_spec(topology: Topology | None, health_path: str = DEFAULT_HEALTH_PATH) -> ProbeSpec:
    """No spec: poll the health URL, routed through the published entry points. Honest but coarse --
    every failure is attributed to the entry tier, because that is all the probe can see."""
    route: tuple[str, ...] = ()
    if topology is not None:
        route = tuple(n for n, node in sorted(topology.services.items())
                      if node.entrypoint and node.chaos_relevant and node.role in ("proxy", "app"))
    op = ProbeRequest(name="health", path=health_path, route=route)
    return ProbeSpec(health_path=health_path, operations={"health": op}, mix=("health",), spike="health")


def load_spec(path: str | Path | None, topology: Topology | None = None) -> ProbeSpec:
    """Read targets/<name>/probe.yaml. Every name it uses is checked here, so a typo fails at load,
    not halfway through an experiment."""
    if not path or not Path(path).is_file():
        return health_only_spec(topology)
    data = yaml.safe_load(Path(path).read_text()) or {}
    health = data.get("health") or {}
    ops = {str(k): _request(str(k), v) for k, v in (data.get("operations") or {}).items()}
    if not ops:
        return health_only_spec(topology, str(health.get("path") or DEFAULT_HEALTH_PATH))
    mix = tuple(str(m) for m in data.get("mix") or ops)
    integrity, spike = str(data.get("integrity") or ""), str(data.get("spike") or next(iter(ops)))
    unknown = sorted({m for m in mix if m not in ops} | ({integrity} - set(ops) - {""}) | ({spike} - set(ops)))
    if unknown:
        raise ValueError(f"{path}: unknown operation(s) {unknown}; defined: {sorted(ops)}")
    return ProbeSpec(health_path=str(health.get("path") or DEFAULT_HEALTH_PATH),
                     health_expect=dict(health.get("expect") or {}),
                     setup=tuple(_request(str(r.get("name", f"setup{i}")), r) for i, r in enumerate(data.get("setup") or [])),
                     operations=ops, mix=mix, integrity=integrity, spike=spike)


def _fill(value: Any, namespace: str, seq: Optional[int] = None) -> Any:
    if isinstance(value, str):
        if value == SEQ and seq is not None:
            return seq
        return value.replace(NAMESPACE, namespace)
    if isinstance(value, dict):
        return {k: _fill(v, namespace, seq) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, namespace, seq) for v in value]
    return value


def health_ok(spec: ProbeSpec, response: requests.Response) -> bool:
    """2xx, and when the spec says so, the JSON body carries the expected values (e.g. status: UP)."""
    if not 200 <= response.status_code < 300:
        return False
    if not spec.health_expect:
        return True
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and all(body.get(k) == v for k, v in spec.health_expect.items())


# ---------------------------------------------------------------------- api
class HttpApiProbe(threading.Thread):
    def __init__(self, base_url: str, spec: ProbeSpec, window: SampleWindow, rps: float = 4.0,
                 namespace: str = "break_point.probe", user: str = "", password: str = "", timeout_s: float = 5.0,
                 log: Log | None = None):
        super().__init__(daemon=True, name="api-probe")
        self.base = base_url.rstrip("/")
        self.spec = spec
        self.auth = (user, password) if user else None
        self.window = window
        self.rps = rps
        self.timeout = timeout_s
        self.namespace = namespace
        self.log = log or (lambda m: None)
        self.seq = 0
        self.last_acked_seq: Optional[int] = None
        self.ready = False
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")
        self._inflight = 0
        self._lock = threading.Lock()

    @property
    def routes(self) -> dict[str, list[str]]:
        return self.spec.routes

    def send(self, op: ProbeRequest, seq: Optional[int] = None, session: Any = requests) -> requests.Response:
        return session.request(op.method, self.base + _fill(op.path, self.namespace), auth=self.auth,
                               timeout=self.timeout, params=_fill(op.params, self.namespace) or None,
                               json=_fill(op.json, self.namespace, seq))

    def spike_request(self) -> tuple[str, str, dict]:
        """(method, url, requests kwargs) of the operation a traffic spike repeats, for chaos.actions."""
        op = self.spec.operations[self.spec.spike]
        return op.method, self.base + _fill(op.path, self.namespace), {
            "auth": self.auth, "params": _fill(op.params, self.namespace) or None,
            "json": _fill(op.json, self.namespace, 0)}

    # ------------------------------------------------------------------ setup
    def ensure_setup(self, attempts: int = 5) -> bool:
        """Send the spec's setup requests (e.g. create the canary). With none, there is nothing to prepare."""
        if not self.spec.setup:
            self.ready = True
            self.last_acked_seq = 0 if self.spec.integrity else None
            return True
        for _ in range(attempts):
            try:
                codes = [self.send(op, seq=0) for op in self.spec.setup]
                bad = [(op.name, r) for op, r in zip(self.spec.setup, codes) if not 200 <= r.status_code < 300]
                if not bad:
                    self.ready = True
                    self.last_acked_seq = 0
                    return True
                name, r = bad[0]
                self.log(f"probe setup {name} returned HTTP {r.status_code}: {r.text[:120]}")
            except requests.RequestException as exc:
                self.log(f"probe setup failed: {type(exc).__name__}")
            time.sleep(2)
        return False

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        if not self.ready and not self.ensure_setup():
            self.log("api probe: setup requests failed, probing anyway")
        interval = 1.0 / self.rps
        mix = self.spec.mix
        i = 0
        nxt = time.time()
        while not self._stop.is_set():
            op = self.spec.operations[mix[i % len(mix)]]
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

    def _do(self, op: ProbeRequest) -> None:
        with self._lock:
            self._inflight += 1
        started = time.time()
        status, err, seq = 0, "", None
        try:
            if op.write:
                with self._lock:
                    self.seq += 1
                    seq = self.seq
            r = self.send(op, seq=seq)
            status = r.status_code
            if op.write and 200 <= status < 300 and seq is not None:
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
        self.window.add(Sample(started, op.name, status, (time.time() - started) * 1000, err))

    # ------------------------------------------------------------------ durability canary
    def integrity_check(self) -> tuple[Optional[bool], str]:
        if not self.spec.integrity:
            return None, "no integrity operation in the probe spec; durability not checked"
        try:
            r = self.send(self.spec.operations[self.spec.integrity])
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


def api_probe(settings, window: SampleWindow, topology: Topology | None = None, log: Log | None = None) -> HttpApiProbe:
    """The probe for the selected target, built from its settings and probe spec."""
    return HttpApiProbe(settings.target_url, load_spec(settings.probe_file, topology), window, settings.probe_rps,
                        settings.probe_namespace, settings.target_user, settings.target_password, log=log)


# ---------------------------------------------------------------------- health
@dataclass(frozen=True)
class HealthSample:
    ts: float
    service: str
    status: Optional[str]      # docker: running / exited / paused / missing
    docker_health: Optional[str]
    alive: Optional[int]       # HTTP code of the liveness path (or the engine ping mapped to 200/503), None = not applicable
    ready: Optional[int]       # HTTP code of the readiness path, None = the service declares none

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
        engine = datastores.get(node.engine) if node.role == "datastore" else None
        try:
            if node.health_port and node.health_path:
                p = node.health_port
                paths = [node.health_path] + ([node.ready_path] if node.ready_path else [])
                code, out = self.docker.shell(svc, "; printf ' '; ".join(
                    f"curl -s -o /dev/null -m 2 -w '%{{http_code}}' localhost:{p}{path}" for path in paths))
                codes = [int(c) if c.isdigit() else 0 for c in out.strip().split()]
                if len(codes) == len(paths):
                    alive, ready = codes[0], (codes[1] if len(codes) > 1 else None)
            elif engine is not None and engine.ping:
                code, _out = self.docker.shell(svc, engine.ping)
                alive = 200 if code == 0 else 503
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
