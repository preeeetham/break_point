"""Code-only safety. Evaluated every second during an injection; any verdict with abort=True triggers
immediate rollback. The model never sees this module's inputs or outputs."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..observe.probes import HealthProbe
from ..observe.window import SampleWindow, percentile
from ..schemas import Thresholds


@dataclass
class Verdict:
    abort: bool
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class SafetyMonitor:
    def __init__(self, thresholds: Thresholds, window: SampleWindow, health: HealthProbe, protected: list[str],
                 stop_requested: Callable[[], bool], injected_at: float):
        self.t = thresholds
        self.window = window
        self.health = health
        self.protected = protected
        self.stop_requested = stop_requested
        self.injected_at = injected_at

    def evaluate(self) -> Verdict:
        if self.stop_requested():
            return Verdict(True, "operator_stop", {})
        recent = self.window.last(self.t.trailing_samples, after=self.injected_at)
        detail: dict[str, Any] = {"trailing_samples": len(recent)}
        if len(recent) >= max(5, self.t.trailing_samples // 2):
            err = 1 - sum(1 for s in recent if s.ok) / len(recent)
            p95 = percentile([s.latency_ms for s in recent if s.ok], 95)
            detail.update({"error_rate": round(err, 3), "p95_ms": p95})
            if err > self.t.abort_error_rate_above:
                return Verdict(True, "error_rate", {**detail, "threshold": self.t.abort_error_rate_above})
            if p95 is not None and p95 > self.t.abort_p95_ms_above:
                return Verdict(True, "latency", {**detail, "threshold_ms": self.t.abort_p95_ms_above})
        for svc in self.protected:
            n = self.health.consecutive_failures(svc)
            if n >= self.t.non_target_health_failures_in_a_row:
                return Verdict(True, f"blast_radius:{svc}", {**detail, "consecutive_failures": n})
        return Verdict(False, None, detail)


class Watchdog:
    """Independent timer: fires `on_fire` even if the experiment loop is stuck in a blocking call."""

    def __init__(self, seconds: float, on_fire: Callable[[], None]):
        self.seconds = seconds
        self.fired = False
        self._on_fire = on_fire
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        self.fired = True
        try:
            self._on_fire()
        except Exception:
            pass

    def arm(self) -> None:
        self._timer.start()

    def disarm(self) -> None:
        self._timer.cancel()
