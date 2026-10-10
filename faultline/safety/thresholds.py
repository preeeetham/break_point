from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..schemas import Thresholds


def load_policy(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text()) or {}


def thresholds_from(policy: dict[str, Any], baseline_p95_ms: float | None) -> Thresholds:
    a, r = policy.get("abort", {}), policy.get("recovery", {})
    p95 = baseline_p95_ms or 0.0
    return Thresholds(
        abort_error_rate_above=float(a.get("probe_error_rate_above", 0.3)),
        abort_p95_ms_above=max(float(a.get("p95_latency_ms_floor", 3000)), p95 * float(a.get("p95_latency_factor_of_baseline", 5))),
        trailing_samples=int(a.get("trailing_samples", 10)),
        non_target_health_failures_in_a_row=int(a.get("non_target_health_failures_in_a_row", 3)),
        max_experiment_seconds=int(a.get("max_experiment_seconds", 90)),
        watchdog_grace_seconds=int(a.get("watchdog_grace_seconds", 15)),
        recovery_success_rate_at_least=float(r.get("success_rate_at_least", 0.95)),
        recovery_p95_ms_below=max(float(r.get("p95_latency_ms_floor", 300)), p95 * float(r.get("p95_latency_factor_of_baseline", 2))),
        recovery_consecutive_checks=int(r.get("consecutive_checks", 3)),
        recovery_timeout_seconds=int(r.get("timeout_seconds", 240)),
        baseline_p95_ms=baseline_p95_ms,
    )
