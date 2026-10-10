"""Apply / revert implementations for every failure type. Each `apply` returns a Handle whose `revert()`
is idempotent and safe to call from the watchdog thread, the guardian, or a finally block."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .docker_ctl import DockerController

Log = Callable[[str], None]


@dataclass
class Context:
    docker: DockerController
    service: str
    container: str
    experiment_id: str
    params: dict[str, Any]
    settings: Any
    log: Log


class Handle:
    def __init__(self, ctx: Context, description: str):
        self.ctx = ctx
        self.description = description
        self.reverted = False
        self.auto_restarted: bool | None = None
        self._lock = threading.Lock()

    def finished(self) -> bool:
        return False

    def revert(self) -> None:
        with self._lock:
            if self.reverted:
                return
            self.reverted = True
        self._revert()

    def _revert(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


# ---------------------------------------------------------------------- container lifecycle
class CrashHandle(Handle):
    def __init__(self, ctx, description, previous_policy: str | None, pid_before: int, started_before: str | None):
        super().__init__(ctx, description)
        self.previous_policy = previous_policy
        self.started_before = started_before

    def _revert(self) -> None:
        d, svc = self.ctx.docker, self.ctx.service
        if self.previous_policy is not None:
            d.set_restart_policy(svc, self.previous_policy)
        st = d.state(svc)
        self.auto_restarted = st.get("status") == "running" and st.get("started_at") != self.started_before
        if st.get("status") != "running":
            d.start(svc)


def apply_container_crash(ctx: Context) -> Handle:
    st = ctx.docker.state(ctx.service)
    prev = None
    if ctx.params.get("block_restart"):
        prev = ctx.docker.set_restart_policy(ctx.service, "no")
    ctx.docker.kill(ctx.service)
    return CrashHandle(ctx, f"SIGKILL {ctx.container}" + (" (auto-restart blocked)" if prev else ""), prev, 0, st.get("started_at"))


class StopHandle(Handle):
    def _revert(self) -> None:
        if not self.ctx.docker.is_running(self.ctx.service):
            self.ctx.docker.start(self.ctx.service)


def apply_container_stop(ctx: Context) -> Handle:
    ctx.docker.stop(ctx.service, timeout=10)
    return StopHandle(ctx, f"docker stop {ctx.container}")


class PauseHandle(Handle):
    def _revert(self) -> None:
        self.ctx.docker.unpause(self.ctx.service)


def apply_container_pause(ctx: Context) -> Handle:
    ctx.docker.pause(ctx.service)
    return PauseHandle(ctx, f"docker pause {ctx.container}")


# ---------------------------------------------------------------------- network
class PartitionHandle(Handle):
    def __init__(self, ctx, description, aliases: list[str]):
        super().__init__(ctx, description)
        self.aliases = aliases

    def _revert(self) -> None:
        d = self.ctx.docker
        if not d.is_connected(self.ctx.service):
            d.connect(self.ctx.service, self.aliases)


def apply_network_partition(ctx: Context) -> Handle:
    aliases = ctx.docker.network_aliases(ctx.service)
    ctx.docker.disconnect(ctx.service)
    return PartitionHandle(ctx, f"disconnected {ctx.container} from {ctx.docker.network_name} (aliases {aliases})", aliases)


class PumbaHandle(Handle):
    def __init__(self, ctx, description, helper):
        super().__init__(ctx, description)
        self.helper = helper

    def finished(self) -> bool:
        try:
            self.helper.reload()
            return self.helper.status != "running"
        except Exception:
            return True

    def _revert(self) -> None:
        d = self.ctx.docker
        d.remove_helper(self.helper)
        # belt and braces: pumba cleans up on SIGTERM, but a stale qdisc would silently poison later experiments
        clear_qdisc(d, self.ctx.settings.tc_image, self.ctx.container)


def clear_qdisc(docker_ctl: DockerController, tc_image: str, container_name: str) -> bool:
    """Remove any netem qdisc from a container's eth0 via the nettools image (its entrypoint is not tc)."""
    try:
        docker_ctl.client.containers.run(tc_image, ["qdisc", "del", "dev", "eth0", "root"], entrypoint="tc",
                                         network_mode=f"container:{container_name}", cap_add=["NET_ADMIN"], remove=True)
        return True
    except Exception:
        return False


def _pumba(ctx: Context, netem_args: list[str], description: str) -> Handle:
    s = ctx.settings
    for img in (s.pumba_image, s.tc_image):
        if not ctx.docker.has_image(img) and not ctx.docker.pull(img):
            raise RuntimeError(f"required image {img} is not available (no network?)")
    d = int(ctx.params["duration_s"])
    cmd = ["--log-level", "info", "netem", "--duration", f"{d}s", "--tc-image", s.tc_image, "--interface", "eth0", *netem_args, ctx.container]
    helper = ctx.docker.run_helper(s.pumba_image, cmd, name=f"faultline-pumba-{ctx.experiment_id[:8]}-{ctx.service}",
                                   volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}})
    time.sleep(2)
    helper.reload()
    if helper.status != "running":
        logs = helper.logs().decode(errors="replace")[-400:]
        ctx.docker.remove_helper(helper)
        raise RuntimeError(f"pumba exited immediately: {logs}")
    return PumbaHandle(ctx, description, helper)


def apply_network_delay(ctx: Context) -> Handle:
    p = ctx.params
    return _pumba(ctx, ["delay", "--time", str(int(p["delay_ms"])), "--jitter", str(int(p["jitter_ms"])), "--distribution", "normal"],
                  f"netem delay {p['delay_ms']}ms ±{p['jitter_ms']}ms on {ctx.container} for {p['duration_s']}s")


def apply_network_loss(ctx: Context) -> Handle:
    p = ctx.params
    return _pumba(ctx, ["loss", "--percent", str(int(p["loss_percent"]))],
                  f"netem loss {p['loss_percent']}% on {ctx.container} for {p['duration_s']}s")


# ---------------------------------------------------------------------- in-container resource faults
def _kill_marked(ctx: Context, marker: str) -> None:
    script = ("for p in /proc/[0-9]*; do if tr '\\0' ' ' < $p/cmdline 2>/dev/null | grep -q '%s'; then "
              "kill -9 ${p#/proc/} 2>/dev/null; fi; done; true" % marker)
    try:
        ctx.docker.shell(ctx.service, script)
    except Exception as exc:
        ctx.log(f"cleanup of {marker} failed: {exc}")


class MarkedProcessHandle(Handle):
    def __init__(self, ctx, description, marker: str, extra_cleanup: str | None = None):
        super().__init__(ctx, description)
        self.marker = marker
        self.extra_cleanup = extra_cleanup

    def _revert(self) -> None:
        _kill_marked(self.ctx, self.marker)
        if self.extra_cleanup:
            try:
                self.ctx.docker.shell(self.ctx.service, self.extra_cleanup)
            except Exception:
                pass


def apply_cpu_stress(ctx: Context) -> Handle:
    marker = f"faultline-{ctx.experiment_id[:8]}"
    d, n = int(ctx.params["duration_s"]), int(ctx.params["workers"])
    for _ in range(n):
        ctx.docker.shell(ctx.service, f"timeout {d + 5} sh -c 'while :; do :; done' {marker}", detach=True)
    return MarkedProcessHandle(ctx, f"{n} busy-loop workers in {ctx.container} for {d}s", marker)


def apply_db_flood(ctx: Context) -> Handle:
    marker = f"faultline-{ctx.experiment_id[:8]}"
    d, n = int(ctx.params["duration_s"]), int(ctx.params["writers"])
    js = (f"var marker='{marker}'; var s='x'.repeat(2048); "
          "while(true){db.getSiblingDB('faultline').flood.insertOne({t:new Date(),x:Math.random(),s:s})}")
    for _ in range(n):
        ctx.docker.shell(ctx.service, f"timeout {d + 5} mongosh --quiet --eval \"{js}\" >/dev/null 2>&1", detach=True)
    return MarkedProcessHandle(ctx, f"{n} mongosh write-flood loops in {ctx.container} for {d}s", marker,
                               extra_cleanup="mongosh --quiet --eval \"db.getSiblingDB('faultline').flood.drop()\" >/dev/null 2>&1")


class DiskHandle(Handle):
    def __init__(self, ctx, description, path: str):
        super().__init__(ctx, description)
        self.path = path

    def _revert(self) -> None:
        try:
            self.ctx.docker.shell(self.ctx.service, f"rm -f {self.path}")
        except Exception:
            pass


def apply_disk_pressure(ctx: Context) -> Handle:
    mb = int(ctx.params["size_mb"])
    path = f"/tmp/faultline-fill-{ctx.experiment_id[:8]}"
    code, out = ctx.docker.shell(ctx.service, f"fallocate -l {mb}M {path} 2>&1 || dd if=/dev/zero of={path} bs=1M count={mb} 2>&1")
    if code not in (0, None):
        raise RuntimeError(f"could not allocate {mb} MB in {ctx.container}: {out.strip()[:200]}")
    return DiskHandle(ctx, f"{mb} MB allocated at {path} in {ctx.container}", path)


# ---------------------------------------------------------------------- traffic
class TrafficHandle(Handle):
    def __init__(self, ctx, description, stop_event: threading.Event, threads: list[threading.Thread], counters: dict):
        super().__init__(ctx, description)
        self.stop_event = stop_event
        self.threads = threads
        self.counters = counters

    def finished(self) -> bool:
        return not any(t.is_alive() for t in self.threads)

    def _revert(self) -> None:
        self.stop_event.set()


def apply_traffic_spike(ctx: Context) -> Handle:
    s = ctx.settings
    rps, d = float(ctx.params["rps"]), int(ctx.params["duration_s"])
    workers = max(4, min(32, int(rps / 10)))
    stop = threading.Event()
    counters: dict[str, int] = {"sent": 0, "ok": 0}
    lock = threading.Lock()
    url = f"{s.ditto_url.rstrip('/')}/api/2/things/{s.probe_namespace}:canary"
    deadline = time.time() + d

    def worker():
        sess = requests.Session()
        sess.auth = (s.ditto_user, s.ditto_password)
        interval = workers / rps
        nxt = time.time()
        while not stop.is_set() and time.time() < deadline:
            try:
                r = sess.get(url, timeout=5)
                ok = r.status_code == 200
            except requests.RequestException:
                ok = False
            with lock:
                counters["sent"] += 1
                counters["ok"] += int(ok)
            nxt += interval
            delay = nxt - time.time()
            if delay > 0:
                stop.wait(delay)

    threads = [threading.Thread(target=worker, daemon=True, name=f"spike-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    return TrafficHandle(ctx, f"{rps:g} req/s burst via {workers} workers for {d}s", stop, threads, counters)


ACTIONS: dict[str, Callable[[Context], Handle]] = {
    "container_crash": apply_container_crash,
    "container_stop": apply_container_stop,
    "container_pause": apply_container_pause,
    "network_partition": apply_network_partition,
    "network_delay": apply_network_delay,
    "network_loss": apply_network_loss,
    "cpu_stress": apply_cpu_stress,
    "db_flood": apply_db_flood,
    "disk_pressure": apply_disk_pressure,
    "traffic_spike": apply_traffic_spike,
}
