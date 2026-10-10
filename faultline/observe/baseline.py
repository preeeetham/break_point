from __future__ import annotations

import time
from datetime import datetime, timezone

from ..safety.thresholds import thresholds_from
from ..schemas import Baseline
from .probes import HealthProbe, ThingsApiProbe
from .window import SampleWindow


def capture(api: ThingsApiProbe, health: HealthProbe, window: SampleWindow, duration_s: float, ditto_url: str,
            policy: dict, log=print) -> Baseline:
    log(f"capturing steady state for {duration_s:.0f}s ...")
    t0 = time.time()
    while time.time() - t0 < duration_s:
        time.sleep(5)
        st = window.stats_since(t0)
        log(f"  {time.time() - t0:4.0f}s  samples={st.sample_count:4d}  success={100 * (st.success_rate or 0):5.1f}%  p95={st.p95_ms} ms")
    t1 = time.time()
    api_stats = window.stats_since(t0, t1)
    availability = health.availability(t0, t1)
    notes = []
    if api_stats.sample_count == 0:
        notes.append("no API samples collected: check DITTO_URL / credentials")
    elif (api_stats.success_rate or 0) < 0.99:
        notes.append(f"baseline is not clean: success rate {api_stats.success_rate:.1%}; fix this before injecting faults")
    for svc, a in availability.items():
        if a is not None and a < 1:
            notes.append(f"{svc} health was only {a:.0%} available at steady state")
    return Baseline(
        captured_at=datetime.now(timezone.utc), duration_s=round(t1 - t0, 1), ditto_url=ditto_url, api=api_stats,
        health_availability=availability, thresholds=thresholds_from(policy, api_stats.p95_ms), notes=notes,
    )


def to_markdown(b: Baseline) -> str:
    a, t = b.api, b.thresholds
    out = [f"# Steady-state baseline", "", f"Captured {b.captured_at.isoformat(timespec='seconds')} against {b.ditto_url}, {b.duration_s}s.", "",
           f"API: {a.sample_count} requests, success {100 * (a.success_rate or 0):.2f}%, p50 {a.p50_ms} ms, p95 {a.p95_ms} ms, p99 {a.p99_ms} ms.", "",
           "| Operation | Count | Success | p95 ms |", "|---|---|---|---|"]
    for op, s in a.by_operation.items():
        out.append(f"| {op} | {s['count']} | {100 * s['success_rate']:.1f}% | {s['p95_ms']} |")
    out += ["", "| Service | Health availability |", "|---|---|"]
    for svc, v in b.health_availability.items():
        out.append(f"| {svc} | {'-' if v is None else f'{100 * v:.0f}%'} |")
    out += ["", "## Safety thresholds derived for injection", "",
            f"- Abort when trailing error rate > {t.abort_error_rate_above:.0%} (last {t.trailing_samples} requests)",
            f"- Abort when p95 latency > {t.abort_p95_ms_above:.0f} ms",
            f"- Abort when a service outside the hypothesis fails {t.non_target_health_failures_in_a_row} health checks in a row",
            f"- Hard cap {t.max_experiment_seconds}s per experiment (+{t.watchdog_grace_seconds}s watchdog)",
            f"- Recovered when success >= {t.recovery_success_rate_at_least:.0%} and p95 < {t.recovery_p95_ms_below:.0f} ms for {t.recovery_consecutive_checks} consecutive checks",
            "", "## Notes", ""] + ([f"- {n}" for n in b.notes] or ["- none"])
    return "\n".join(out) + "\n"
