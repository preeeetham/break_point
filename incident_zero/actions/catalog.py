"""Runbook actions and parameter validation.

The jury's Action API defines the real list. Until its spec is known, the mock serves MOCK_ACTIONS; the Planner
never uses this list directly but reads whatever `GET /actions` returns, so swapping APIs changes nothing upstream.
`validate` is applied by code to every proposed action, whatever produced it (REQUIREMENTS S5).
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ParamDef(BaseModel):
    type: str = "string"            # string | number | integer | boolean
    required: bool = False
    description: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None


class ActionDef(BaseModel):
    name: str
    description: str
    parameters: dict[str, ParamDef] = {}
    reversible: bool = False
    fixes: list[str] = []           # diagnosis categories this action addresses (a hint for the rule planner)


SERVICE = ParamDef(required=True, description="the compose service to act on")

MOCK_ACTIONS: list[ActionDef] = [
    ActionDef(name="restart_service", description="Restart a service's container (clears a leaked heap, a stuck "
              "process or a bad in-memory cache).", parameters={"service": SERVICE},
              fixes=["resource_memory", "resource_cpu", "application_error"]),
    ActionDef(name="start_service", description="Start a stopped or exited container, or unpause a paused one.",
              parameters={"service": SERVICE}, fixes=["container_down", "dependency_failure"]),
    ActionDef(name="update_resources", description="Change a container's CPU and/or memory limit.",
              parameters={"service": SERVICE,
                          "cpus": ParamDef(type="number", minimum=0.1, maximum=4, description="CPU limit in cores"),
                          "memory_mb": ParamDef(type="integer", minimum=64, maximum=4096, description="memory limit")},
              reversible=True, fixes=["resource_cpu", "resource_memory"]),
    ActionDef(name="clear_network_impairment", description="Remove injected latency/packet loss (tc netem) from a "
              "container's network interface.", parameters={"service": SERVICE}, fixes=["network"]),
    ActionDef(name="reconnect_network", description="Reattach a container that was disconnected from the "
              "application network.", parameters={"service": SERVICE}, fixes=["network", "container_down"]),
    ActionDef(name="rollback_release", description="Roll a service back to its last known good release "
              "(reverts the faulty change in its code path).", parameters={"service": SERVICE}, reversible=True,
              fixes=["application_error", "resource_memory", "resource_cpu", "traffic"]),
]


def validate(action: str, params: dict[str, Any], catalog: list[ActionDef],
             services: Optional[set[str]] = None) -> list[str]:
    """Every problem with a proposed action; empty means it may be shown to a human."""
    defs = {a.name: a for a in catalog}
    a = defs.get(action)
    if a is None:
        return [f"{action!r} is not an allowed action (allowed: {', '.join(sorted(defs))})"]
    errs = [f"unexpected parameter {k!r}" for k in params if k not in a.parameters]
    for name, p in a.parameters.items():
        if name not in params or params[name] in (None, ""):
            if p.required:
                errs.append(f"missing required parameter {name!r}")
            continue
        v = params[name]
        if p.type in ("number", "integer"):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"{name} must be a {p.type}")
                continue
            if p.type == "integer" and not float(v).is_integer():
                errs.append(f"{name} must be an integer")
            if p.minimum is not None and v < p.minimum:
                errs.append(f"{name} below {p.minimum:g}")
            if p.maximum is not None and v > p.maximum:
                errs.append(f"{name} above {p.maximum:g}")
        elif p.type == "boolean" and not isinstance(v, bool):
            errs.append(f"{name} must be true or false")
        elif p.type == "string" and not isinstance(v, str):
            errs.append(f"{name} must be a string")
    svc = params.get("service")
    if services is not None and isinstance(svc, str) and svc not in services:
        errs.append(f"unknown service {svc!r}")
    if action == "update_resources" and not ({"cpus", "memory_mb"} & set(params)):
        errs.append("update_resources needs cpus or memory_mb")
    return errs
