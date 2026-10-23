"""Read-only clients for the four telemetry sources, and endpoint discovery.

  Prometheus   instant and range PromQL over the collector's span metrics, docker_stats and datastore receivers
  Jaeger       trace search and trace detail (Jaeger v2, UI base path /jaeger/ui)
  Logs         OpenSearch (`otel-logs-*`), falling back to `docker logs` when OpenSearch is absent or down
  Docker       container state, restarts, OOM kills, health, limits, networks and live stats; never a write call
  Topology     dependency edges from the stack's own compose file, found through the containers' compose labels

A new backend (Loki, Tempo, Datadog, ...) is one more class with the same methods (REQUIREMENTS N4).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

TIMEOUT = 8.0
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_FILES_LABEL = "com.docker.compose.project.config_files"


class BackendError(RuntimeError):
    pass


# ---------------------------------------------------------------- Prometheus
class Prometheus:
    def __init__(self, url: str, session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()

    def query(self, promql: str, at: Optional[float] = None) -> list[dict]:
        params: dict[str, Any] = {"query": promql}
        if at is not None:
            params["time"] = at
        return self._get("/api/v1/query", params)

    def query_range(self, promql: str, start: float, end: float, step: float) -> list[dict]:
        return self._get("/api/v1/query_range", {"query": promql, "start": start, "end": end, "step": step})

    def _get(self, path: str, params: dict) -> list[dict]:
        if not self.url:
            raise BackendError("Prometheus URL unknown")
        try:
            r = self.http.get(self.url + path, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise BackendError(f"Prometheus unreachable: {exc.__class__.__name__}") from exc
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or body.get("status") != "success":
            raise BackendError(f"Prometheus {r.status_code}: {body.get('error') or r.text[:200]}")
        return body["data"]["result"]

    def vector(self, promql: str, label: str, at: Optional[float] = None) -> dict[str, float]:
        """{label value: number} for an instant vector; NaN and missing labels are dropped."""
        out: dict[str, float] = {}
        for item in self.query(promql, at):
            key = item.get("metric", {}).get(label)
            try:
                v = float(item["value"][1])
            except (KeyError, ValueError, IndexError):
                continue
            if key is not None and v == v:  # v == v filters NaN
                out[key] = v
        return out


# ---------------------------------------------------------------- Jaeger
class Jaeger:
    def __init__(self, url: str, session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        if not self.url:
            raise BackendError("Jaeger URL unknown")
        try:
            r = self.http.get(self.url + path, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise BackendError(f"Jaeger unreachable: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise BackendError(f"Jaeger {r.status_code}: {r.text[:200]}")
        return r.json().get("data")

    def services(self) -> list[str]:
        return sorted(self._get("/api/services") or [])

    def find_traces(self, service: str, operation: Optional[str] = None, errors_only: bool = False,
                    min_duration_ms: Optional[float] = None, lookback_s: float = 900, limit: int = 20) -> list[dict]:
        params: dict[str, Any] = {"service": service, "limit": limit, "lookback": f"{int(lookback_s)}s",
                                  "start": int((time.time() - lookback_s) * 1e6), "end": int(time.time() * 1e6)}
        if operation:
            params["operation"] = operation
        if errors_only:
            params["tags"] = json.dumps({"error": "true"})
        if min_duration_ms:
            params["minDuration"] = f"{int(min_duration_ms)}ms"
        return self._get("/api/traces", params) or []

    def trace(self, trace_id: str) -> Optional[dict]:
        data = self._get(f"/api/traces/{trace_id}") or []
        return data[0] if data else None


# ---------------------------------------------------------------- logs
@dataclass
class LogLine:
    ts: str
    service: str
    severity: str
    body: str
    trace_id: str = ""


class OpenSearchLogs:
    INDEX = "otel-logs-*"
    SERVICE_FIELDS = ("resource.service.name", "resource.attributes.service.name", "resource.attributes.service@name",
                      "service.name", "serviceName")
    SEVERITY_FIELDS = ("severity.text", "severityText", "severity_text", "level")

    def __init__(self, url: str, session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()

    def search(self, service: Optional[str], contains: Optional[str], errors_only: bool, minutes: float,
               limit: int) -> list[LogLine]:
        if not self.url:
            raise BackendError("OpenSearch URL unknown")
        must: list[dict] = [{"range": {"@timestamp": {"gte": f"now-{int(minutes * 60)}s"}}}]
        if service:
            must.append({"bool": {"should": [{"match_phrase": {f: service}} for f in self.SERVICE_FIELDS],
                                  "minimum_should_match": 1}})
        if contains:
            must.append({"query_string": {"query": f"\"{contains}\"", "fields": ["body", "body.*", "message"]}})
        if errors_only:
            must.append({"bool": {"should": [{"terms": {f: ["ERROR", "error", "Error", "FATAL", "fatal", "WARN",
                                                            "warn", "Warning", "WARNING"]}} for f in self.SEVERITY_FIELDS]
                                  + [{"range": {"severity.number": {"gte": 13}}}, {"range": {"severityNumber": {"gte": 13}}}],
                                  "minimum_should_match": 1}})
        q = {"size": limit, "sort": [{"@timestamp": "desc"}], "query": {"bool": {"must": must}}}
        try:
            r = self.http.post(f"{self.url}/{self.INDEX}/_search", json=q, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise BackendError(f"OpenSearch unreachable: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise BackendError(f"OpenSearch {r.status_code}: {r.text[:200]}")
        return [self._line(h.get("_source") or {}) for h in (r.json().get("hits") or {}).get("hits") or []]

    def _line(self, src: dict) -> LogLine:
        def pick(*paths: str) -> str:
            for p in paths:
                cur: Any = src
                for part in p.split("."):
                    cur = cur.get(part) if isinstance(cur, dict) else None
                    if cur is None:
                        break
                if cur is None and p in src:
                    cur = src[p]
                if cur not in (None, ""):
                    return str(cur)
            return ""
        body = pick("body", "message", "body.text")
        return LogLine(ts=pick("@timestamp", "time", "observedTimestamp"), service=pick(*self.SERVICE_FIELDS),
                       severity=pick(*self.SEVERITY_FIELDS) or "", body=body, trace_id=pick("traceId", "trace_id"))


ERROR_WORDS = ("error", "exception", "fail", "fatal", "panic", "refused", "timeout", "timed out", "unavailable",
               "oom", "killed", "warn")


class DockerLogs:
    """Fallback log source: the container's own stdout/stderr."""

    def __init__(self, docker: "DockerReader"):
        self.docker = docker

    def search(self, service: Optional[str], contains: Optional[str], errors_only: bool, minutes: float,
               limit: int) -> list[LogLine]:
        if not service:
            raise BackendError("container logs need a service")
        c = self.docker.container(service)
        if c is None:
            raise BackendError(f"no container for {service}")
        raw = c.logs(since=int(time.time() - minutes * 60), timestamps=True, tail=2000).decode("utf-8", "replace")
        out: list[LogLine] = []
        for ln in raw.splitlines():
            ts, _, body = ln.partition(" ")
            low = body.lower()
            if contains and contains.lower() not in low:
                continue
            if errors_only and not any(w in low for w in ERROR_WORDS):
                continue
            out.append(LogLine(ts=ts[:23], service=service, severity="", body=body.strip()[:500]))
        return out[-limit:][::-1]


class Logs:
    def __init__(self, primary: Optional[OpenSearchLogs], fallback: DockerLogs):
        self.primary, self.fallback = primary, fallback

    def search(self, service: Optional[str], contains: Optional[str] = None, errors_only: bool = True,
               minutes: float = 10, limit: int = 20) -> tuple[str, list[LogLine], str]:
        """(source used, lines, note). Falls back to container logs when OpenSearch fails or finds nothing."""
        note = ""
        if self.primary is not None:
            try:
                lines = self.primary.search(service, contains, errors_only, minutes, limit)
                if lines:
                    return "opensearch", lines, ""
                note = "OpenSearch returned no lines; "
            except BackendError as exc:
                note = f"{exc}; "
        lines = self.fallback.search(service, contains, errors_only, minutes, limit)
        return "docker", lines, note + "used container logs"


# ---------------------------------------------------------------- Docker (read-only)
def _parse_ts(s: str) -> Optional[datetime]:
    if not s or s.startswith("0001"):
        return None
    s = s.rstrip("Z")
    s = s.split(".")[0]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class DockerReader:
    """Only inspect, stats and logs. The agent process never stops, starts or changes a container: that is the
    Action API's job (REQUIREMENTS F6.1)."""

    def __init__(self, project: str, client: Any = None):
        self.project = project
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def containers(self) -> dict[str, Any]:
        out = {}
        for c in self.client.containers.list(all=True, filters={"label": f"{COMPOSE_PROJECT_LABEL}={self.project}"}):
            svc = (c.labels or {}).get(COMPOSE_SERVICE_LABEL)
            if svc:
                out[svc] = c
        return out

    def container(self, service: str):
        return self.containers().get(service)

    def status(self, c) -> dict[str, Any]:
        c.reload()
        a = c.attrs
        st = a.get("State") or {}
        hc = a.get("HostConfig") or {}
        started = _parse_ts(st.get("StartedAt", ""))
        health = st.get("Health") or {}
        nets = list(((a.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        return {
            "service": (c.labels or {}).get(COMPOSE_SERVICE_LABEL, c.name),
            "container": c.name,
            "state": st.get("Status"),
            "running": bool(st.get("Running")),
            "paused": bool(st.get("Paused")),
            "restarting": bool(st.get("Restarting")),
            "exit_code": st.get("ExitCode"),
            "oom_killed": bool(st.get("OOMKilled")),
            "restart_count": a.get("RestartCount", 0),
            "uptime_s": round(time.time() - started.timestamp()) if started and st.get("Running") else None,
            "finished_at": st.get("FinishedAt") if not st.get("Running") else None,
            "health": health.get("Status"),
            "health_failing_streak": health.get("FailingStreak"),
            "cpu_limit": (hc.get("NanoCpus") or 0) / 1e9 or None,
            "memory_limit_mb": round((hc.get("Memory") or 0) / 2**20) or None,
            "networks": nets,
        }

    def stats(self, c) -> dict[str, Any]:
        s = c.stats(stream=False)
        cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
        cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or 1
        mem = s.get("memory_stats") or {}
        usage = mem.get("usage", 0) - ((mem.get("stats") or {}).get("inactive_file", 0))
        limit = mem.get("limit") or 0
        thr, pthr = cpu.get("throttling_data") or {}, pre.get("throttling_data") or {}
        nets = s.get("networks") or {}
        return {
            "cpu_percent": round(cpu_delta / sys_delta * ncpu * 100, 1) if sys_delta > 0 else 0.0,
            "memory_mb": round(usage / 2**20, 1),
            "memory_limit_mb": round(limit / 2**20, 1) if limit else None,
            "memory_percent": round(usage / limit * 100, 1) if limit else None,
            "throttled_periods": thr.get("throttled_periods", 0),
            "throttled_periods_delta": thr.get("throttled_periods", 0) - pthr.get("throttled_periods", 0),
            "net_rx_errors": sum(n.get("rx_errors", 0) for n in nets.values()),
            "net_tx_dropped": sum(n.get("tx_dropped", 0) for n in nets.values()),
            "pids": (s.get("pids_stats") or {}).get("current"),
        }

    def published_port(self, service: str, container_port: int) -> Optional[int]:
        c = self.container(service)
        if c is None:
            return None
        c.reload()
        ports = ((c.attrs.get("NetworkSettings") or {}).get("Ports") or {}).get(f"{container_port}/tcp") or []
        for p in ports:
            if p.get("HostPort"):
                return int(p["HostPort"])
        return None

    def compose_files(self) -> list[str]:
        for c in self.containers().values():
            files = (c.labels or {}).get(COMPOSE_FILES_LABEL)
            if files:
                return [f for f in files.split(",") if f]
        return []


# ---------------------------------------------------------------- topology
@dataclass
class Topology:
    edges: list[tuple[str, str, str]] = field(default_factory=list)   # (caller, callee, evidence)
    roles: dict[str, str] = field(default_factory=dict)

    def callees(self, svc: str) -> list[str]:
        return sorted({d for s, d, _ in self.edges if s == svc})

    def callers(self, svc: str) -> list[str]:
        return sorted({s for s, d, _ in self.edges if d == svc})


def load_topology(compose_paths: list[str]) -> Topology:
    """Reuses Break Point's compose parser on the first compose file (the demo's base compose.yaml)."""
    from break_point.topology.compose import load_compose, service_nodes, static_edges

    topo = Topology()
    for path in compose_paths[:1]:
        if not Path(path).is_file():
            continue
        compose = load_compose(path)
        nodes = service_nodes(compose)
        topo.roles = {n: node.role for n, node in nodes.items()}
        topo.edges = [(e.src, e.dst, e.evidence) for e in static_edges(compose, nodes)]
    return topo


# ---------------------------------------------------------------- discovery
@dataclass
class Endpoints:
    prometheus: str = ""
    jaeger: str = ""
    opensearch: str = ""
    notes: list[str] = field(default_factory=list)


def discover(settings, docker: DockerReader) -> Endpoints:
    """Configured URLs win; otherwise the published host ports of the running stack; Jaeger also through Envoy."""
    ep = Endpoints(settings.prometheus_url, settings.jaeger_url, settings.opensearch_url)
    try:
        if not ep.prometheus:
            port = docker.published_port("prometheus", 9090)
            ep.prometheus = f"http://127.0.0.1:{port}" if port else ""
        if not ep.jaeger:
            port = docker.published_port("jaeger", 16686)
            ep.jaeger = f"http://127.0.0.1:{port}/jaeger/ui" if port else settings.frontend_url.rstrip("/") + "/jaeger/ui"
        if not ep.opensearch:
            port = docker.published_port("opensearch", 9200)
            ep.opensearch = f"http://127.0.0.1:{port}" if port else ""
    except Exception as exc:  # Docker not running: keep whatever was configured
        ep.notes.append(f"endpoint discovery failed: {exc.__class__.__name__}: {exc}")
    for name in ("prometheus", "jaeger", "opensearch"):
        if not getattr(ep, name):
            ep.notes.append(f"{name}: no endpoint")
    return ep
