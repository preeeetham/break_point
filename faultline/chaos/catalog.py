"""The failure catalog: what can be broken, on which services, within which bounds. Pure data plus
clamping logic; the actual mechanics live in actions.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schemas import ServiceNode


@dataclass(frozen=True)
class Param:
    name: str
    default: Any
    lo: float | None = None
    hi: float | None = None
    unit: str = ""
    desc: str = ""


@dataclass(frozen=True)
class FailureType:
    key: str
    title: str
    slide_category: str
    description: str
    params: tuple[Param, ...]
    roles: tuple[str, ...] = ("app", "datastore", "proxy")
    requires_images: tuple[str, ...] = ()
    degrade_only: bool = False      # True = does not take the target fully away (allowed on protected services)
    severity_hint: float = 1.0      # how much API damage to expect at most, by default
    expected_recovery_s: float = 20.0
    notes: list[str] = field(default_factory=list)


DUR = lambda d, lo=5, hi=90: Param("duration_s", d, lo, hi, "s", "how long the fault stays applied")  # noqa: E731

CATALOG: dict[str, FailureType] = {
    "container_crash": FailureType(
        "container_crash", "Container crash (SIGKILL)", "Service or container crash",
        "Kill the process without warning. With compose `restart: always` Docker restarts it, so this measures "
        "crash detection + cold-start + cluster rejoin time. `block_restart=true` keeps it down for the full duration.",
        (DUR(30, 10, 90), Param("block_restart", False, desc="disable Docker auto-restart during the experiment")),
        severity_hint=1.0, expected_recovery_s=90,
    ),
    "container_stop": FailureType(
        "container_stop", "Graceful stop (SIGTERM)", "Failed dependency",
        "Stop the container cleanly and keep it down until rollback: a sustained dependency outage with coordinated shutdown.",
        (DUR(30, 10, 90),), severity_hint=1.0, expected_recovery_s=90,
    ),
    "container_pause": FailureType(
        "container_pause", "Process freeze (SIGSTOP)", "Service or container crash",
        "Freeze every process in the container. TCP connections stay open, so callers hang instead of failing fast; "
        "a realistic 'hung service' scenario.",
        (DUR(20, 5, 60),), severity_hint=1.0, expected_recovery_s=15,
    ),
    "network_delay": FailureType(
        "network_delay", "Network latency (tc netem)", "Slow or lossy network",
        "Add delay + jitter to all traffic leaving the container via Pumba/tc netem (runs as a sidecar, nothing installed in the target).",
        (DUR(45, 10, 90), Param("delay_ms", 800, 100, 3000, "ms"), Param("jitter_ms", 200, 0, 1000, "ms")),
        requires_images=("pumba", "tc"), degrade_only=True, severity_hint=0.3, expected_recovery_s=10,
    ),
    "network_loss": FailureType(
        "network_loss", "Packet loss (tc netem)", "Slow or lossy network",
        "Drop a percentage of packets leaving the container.",
        (DUR(45, 10, 90), Param("loss_percent", 30, 5, 80, "%")),
        requires_images=("pumba", "tc"), degrade_only=True, severity_hint=0.4, expected_recovery_s=10,
    ),
    "network_partition": FailureType(
        "network_partition", "Network partition", "Failed dependency",
        "Disconnect the container from the compose network (process keeps running, nobody can reach it); reconnect with "
        "the same aliases on rollback. No extra tooling.",
        (DUR(30, 5, 90),), severity_hint=1.0, expected_recovery_s=20,
    ),
    "cpu_stress": FailureType(
        "cpu_stress", "CPU starvation", "Overloaded database",
        "Run busy-loop workers inside the container (bounded by `timeout`, tagged so they can be killed early).",
        (DUR(45, 10, 90), Param("workers", 2, 1, 4)), degrade_only=True, severity_hint=0.2, expected_recovery_s=10,
    ),
    "disk_pressure": FailureType(
        "disk_pressure", "Disk pressure", "Full disk",
        "Allocate a large file in the container's writable layer. Bounded (max 1 GiB) because the Docker VM disk is "
        "shared with every other container on the host.",
        (DUR(30, 10, 90), Param("size_mb", 256, 64, 1024, "MB")), degrade_only=True, severity_hint=0.05, expected_recovery_s=5,
    ),
    "db_flood": FailureType(
        "db_flood", "Database write flood", "Overloaded database",
        "Parallel mongosh insert loops inside the MongoDB container: real write load competing with Ditto's own traffic.",
        (DUR(45, 10, 90), Param("writers", 2, 1, 4)), roles=("datastore",), degrade_only=True, severity_hint=0.3,
        expected_recovery_s=15,
    ),
    "traffic_spike": FailureType(
        "traffic_spike", "Traffic spike", "Sudden traffic spike",
        "Burst of concurrent API requests against nginx -> gateway from the host, on top of the probe traffic.",
        (DUR(30, 10, 90), Param("rps", 150, 20, 600, "req/s")), roles=("proxy", "app"), degrade_only=True,
        severity_hint=0.2, expected_recovery_s=10,
    ),
}

TRAFFIC_TARGETS = ("nginx", "gateway")


def applicable(ft: FailureType, node: ServiceNode) -> bool:
    if not node.chaos_relevant or node.role not in ft.roles:
        return False
    if ft.key == "traffic_spike":
        return node.name in TRAFFIC_TARGETS
    if ft.key == "db_flood":
        return "mongo" in node.image
    return True


def clamp(ft: FailureType, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    notes: list[str] = []
    for p in ft.params:
        raw = params.get(p.name, p.default)
        if isinstance(p.default, bool):
            out[p.name] = str(raw).lower() in ("1", "true", "yes", "on") if not isinstance(raw, bool) else raw
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            notes.append(f"{p.name}={raw!r} invalid, using default {p.default}")
            val = float(p.default)
        if p.lo is not None and val < p.lo:
            notes.append(f"{p.name} raised from {val:g} to minimum {p.lo:g}")
            val = p.lo
        if p.hi is not None and val > p.hi:
            notes.append(f"{p.name} lowered from {val:g} to maximum {p.hi:g}")
            val = p.hi
        out[p.name] = int(val) if float(val).is_integer() else val
    unknown = set(params) - {p.name for p in ft.params}
    if unknown:
        notes.append(f"ignored unknown parameters: {sorted(unknown)}")
    return out, notes


def reproduce_commands(ft_key: str, container: str, params: dict[str, Any], network: str, pumba: str, tc: str) -> list[str]:
    d = params.get("duration_s", 30)
    if ft_key == "container_crash":
        pre = [f"docker update --restart=no {container}"] if params.get("block_restart") else []
        return pre + [f"docker kill {container}", f"sleep {d}", f"docker update --restart=always {container}", f"docker start {container}"]
    if ft_key == "container_stop":
        return [f"docker stop {container}", f"sleep {d}", f"docker start {container}"]
    if ft_key == "container_pause":
        return [f"docker pause {container}", f"sleep {d}", f"docker unpause {container}"]
    if ft_key == "network_delay":
        return [f"docker run --rm -v /var/run/docker.sock:/var/run/docker.sock {pumba} netem --duration {d}s --tc-image {tc} "
                f"delay --time {params.get('delay_ms')} --jitter {params.get('jitter_ms')} {container}"]
    if ft_key == "network_loss":
        return [f"docker run --rm -v /var/run/docker.sock:/var/run/docker.sock {pumba} netem --duration {d}s --tc-image {tc} "
                f"loss --percent {params.get('loss_percent')} {container}"]
    if ft_key == "network_partition":
        return [f"docker network disconnect {network} {container}", f"sleep {d}",
                f"docker network connect --alias <original aliases> {network} {container}"]
    if ft_key == "cpu_stress":
        return [f"docker exec -d {container} sh -c 'timeout {d} sh -c \"while :; do :; done\"'  # x{params.get('workers')}"]
    if ft_key == "disk_pressure":
        return [f"docker exec {container} fallocate -l {params.get('size_mb')}M /tmp/fill", f"sleep {d}", f"docker exec {container} rm /tmp/fill"]
    if ft_key == "db_flood":
        return [f"docker exec -d {container} sh -c 'timeout {d} mongosh --quiet --eval \"while(true){{db.flood.insertOne({{x:Math.random()}})}}\"'  # x{params.get('writers')}"]
    if ft_key == "traffic_spike":
        return [f"# {params.get('rps')} req/s for {d}s against $DITTO_URL/api/2/things/faultline.probe:canary (e.g. hey -z {d}s -q {params.get('rps')} -c 16)"]
    return []
