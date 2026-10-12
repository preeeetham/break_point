"""Guardian agent: a second, independent kill switch. Runs as its own process, shares nothing with the
injector but the blackboard files, and will restore the sandbox even if the injector is dead.

- any sandbox container that is not running past its allowed window is started/unpaused
- chaos helper containers (pumba) are removed once their experiment deadline has passed
- a stale ACTIVE_EXPERIMENT lock is cleared and its target restored
- the STOP file means: restore everything, now
"""
from __future__ import annotations

import time

from ..chaos.actions import clear_qdisc
from ..chaos.docker_ctl import DockerController
from ..reporting.store import ReportStore

DOWN_GRACE_S = 20.0


def run(settings, store: ReportStore, docker: DockerController, interval_s: float = 5.0, log=print) -> None:
    topo = store.topology()
    watched = topo.relevant_services() if topo else None
    down_since: dict[str, float] = {}
    log(f"guardian armed: project {settings.compose_project}, watching {watched or 'all project containers'} every {interval_s:.0f}s")
    while True:
        actions: list[str] = []
        now = time.time()
        stop = store.stop_requested()
        lock = store.active() or {}
        lock_target = lock.get("target_service")
        lock_deadline = float(lock.get("deadline", 0) or 0)
        try:
            containers = docker.containers()
            for svc, c in containers.items():
                if watched and svc not in watched:
                    continue
                c.reload()
                paused = c.attrs.get("State", {}).get("Paused", False)
                if c.status == "running" and not paused:
                    down_since.pop(svc, None)
                    continue
                down_since.setdefault(svc, now)
                # a fix being applied recreates several services at once (lock kind "fix_apply", "services")
                expected = ((svc == lock_target or svc in (lock.get("services") or [])) and now < lock_deadline
                            and not stop)
                if expected:
                    continue
                if stop or now - down_since[svc] > DOWN_GRACE_S:
                    try:
                        if paused:
                            c.unpause()
                        else:
                            c.start()
                        actions.append(f"restored {svc} ({'paused' if paused else c.status} for {now - down_since[svc]:.0f}s)")
                    except Exception as exc:
                        actions.append(f"failed to restore {svc}: {exc}")
            if not stop and lock and now > lock_deadline + 30:
                store.clear_active()
                actions.append(f"cleared stale lock for {lock.get('experiment_id')}")
            for h in docker.helper_containers():
                if stop or not lock or now > lock_deadline:
                    docker.remove_helper(h)
                    actions.append(f"removed helper {h.name}")
                    target = h.name.split("-", 3)[-1] if h.name.count("-") >= 3 else None
                    if target and target in containers:
                        clear_qdisc(docker, settings.tc_image, containers[target].name)
            if stop:
                for svc in list(containers):
                    if watched and svc not in watched:
                        continue
                    if not docker.is_connected(svc):
                        try:
                            docker.connect(svc, [svc])
                            actions.append(f"reconnected {svc} to {docker.network_name}")
                        except Exception as exc:
                            actions.append(f"reconnect {svc} failed: {exc}")
        except Exception as exc:
            actions.append(f"guardian error: {type(exc).__name__}: {exc}")
        for a in actions:
            log(f"guardian: {a}")
            store.append_log("guardian", f"{time.strftime('%H:%M:%S')} {a}")
        store.write_heartbeat("guardian", {"stop": stop, "active": lock.get("experiment_id"), "actions": actions,
                                           "down": {k: round(now - v) for k, v in down_since.items()}})
        time.sleep(interval_s)
