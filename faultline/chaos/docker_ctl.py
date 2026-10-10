"""Thin, boring wrappers around docker-py. Everything the chaos actions and the guardian do to
containers goes through here, scoped to one compose project label so nothing outside the sandbox
can ever be touched."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import docker
from docker.errors import APIError, NotFound

HELPER_PREFIX = "faultline-"


class DockerController:
    def __init__(self, project: str, network: str | None = None):
        self.project = project
        self.network_name = network or f"{project}_default"
        self.client = docker.from_env()

    # ------------------------------------------------------------ lookup
    def containers(self) -> dict[str, Any]:
        out = {}
        for c in self.client.containers.list(all=True, filters={"label": f"com.docker.compose.project={self.project}"}):
            svc = c.labels.get("com.docker.compose.service")
            if svc:
                out[svc] = c
        return out

    def container(self, service: str):
        c = self.containers().get(service)
        if c is None:
            raise RuntimeError(f"no container for service '{service}' in project '{self.project}'")
        c.reload()
        return c

    def state(self, service: str) -> dict[str, Any]:
        try:
            c = self.container(service)
        except RuntimeError:
            return {"status": "missing", "health": None, "restart_count": 0, "started_at": None, "exists": False}
        st = c.attrs.get("State", {})
        return {
            "status": st.get("Status"),
            "health": (st.get("Health") or {}).get("Status"),
            "restart_count": c.attrs.get("RestartCount", 0),
            "started_at": st.get("StartedAt"),
            "paused": st.get("Paused", False),
            "exists": True,
            "name": c.name,
        }

    def is_running(self, service: str) -> bool:
        return self.state(service).get("status") == "running"

    # ------------------------------------------------------------ lifecycle
    def kill(self, service: str, signal: str = "SIGKILL") -> None:
        self.container(service).kill(signal=signal)

    def stop(self, service: str, timeout: int = 10) -> None:
        self.container(service).stop(timeout=timeout)

    def start(self, service: str) -> None:
        c = self.container(service)
        if c.attrs["State"].get("Paused"):
            c.unpause()
        elif c.status != "running":
            c.start()

    def pause(self, service: str) -> None:
        self.container(service).pause()

    def unpause(self, service: str) -> None:
        c = self.container(service)
        if c.attrs["State"].get("Paused"):
            c.unpause()

    def restart(self, service: str, timeout: int = 10) -> None:
        self.container(service).restart(timeout=timeout)

    def set_restart_policy(self, service: str, name: str) -> str:
        c = self.container(service)
        previous = c.attrs["HostConfig"].get("RestartPolicy", {}).get("Name", "no") or "no"
        self.client.api.update_container(c.id, restart_policy={"Name": name, "MaximumRetryCount": 0})
        return previous

    def wait_running(self, service: str, timeout_s: float = 60) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.is_running(service):
                return True
            time.sleep(1)
        return False

    # ------------------------------------------------------------ exec / logs
    def exec(self, service: str, cmd: list[str] | str, detach: bool = False, user: str | None = None) -> tuple[int | None, str]:
        c = self.container(service)
        res = c.exec_run(cmd, detach=detach, user=user or "")
        if detach:
            return None, ""
        out = res.output.decode(errors="replace") if isinstance(res.output, (bytes, bytearray)) else str(res.output)
        return res.exit_code, out

    def shell(self, service: str, script: str, detach: bool = False) -> tuple[int | None, str]:
        return self.exec(service, ["sh", "-c", script], detach=detach)

    def logs(self, service: str, since: float, tail: int = 80) -> str:
        try:
            c = self.container(service)
            raw = c.logs(since=datetime.fromtimestamp(since, tz=timezone.utc), tail=tail, timestamps=True)
            return raw.decode(errors="replace")
        except (RuntimeError, APIError, NotFound) as exc:
            return f"<logs unavailable: {exc}>"

    # ------------------------------------------------------------ network
    def network(self):
        return self.client.networks.get(self.network_name)

    def network_aliases(self, service: str) -> list[str]:
        c = self.container(service)
        nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
        info = nets.get(self.network_name) or {}
        aliases = list(info.get("Aliases") or [])
        if service not in aliases:
            aliases.append(service)
        return [a for a in aliases if not c.id.startswith(a)]

    def disconnect(self, service: str) -> None:
        self.network().disconnect(self.container(service), force=True)

    def connect(self, service: str, aliases: list[str]) -> None:
        self.network().connect(self.container(service), aliases=aliases)

    def is_connected(self, service: str) -> bool:
        c = self.container(service)
        return self.network_name in c.attrs.get("NetworkSettings", {}).get("Networks", {})

    # ------------------------------------------------------------ helpers (pumba, tc cleanup)
    def pull(self, image: str) -> bool:
        try:
            self.client.images.pull(image)
            return True
        except APIError:
            return False

    def has_image(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except (NotFound, APIError):
            return False

    def run_helper(self, image: str, command: list[str], name: str, **kwargs):
        return self.client.containers.run(image, command, name=name, detach=True, labels={"faultline.helper": "1"}, **kwargs)

    def helper_containers(self) -> list:
        return self.client.containers.list(all=True, filters={"label": "faultline.helper=1"})

    def remove_helper(self, container) -> None:
        try:
            container.reload()
            if container.status == "running":
                container.kill(signal="SIGTERM")
                try:
                    container.wait(timeout=8)
                except Exception:
                    container.kill()
            container.remove(force=True)
        except (NotFound, APIError):
            pass
