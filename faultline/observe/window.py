from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from ..schemas import WindowStats


@dataclass(frozen=True)
class Sample:
    ts: float
    op: str
    status: int
    latency_ms: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(vals[lo], 2)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 2)


def stats(samples: list[Sample]) -> WindowStats:
    if not samples:
        return WindowStats()
    ok = [s for s in samples if s.ok]
    lat = [s.latency_ms for s in ok]
    by_op: dict[str, dict] = {}
    for op in sorted({s.op for s in samples}):
        sub = [s for s in samples if s.op == op]
        by_op[op] = {
            "count": len(sub),
            "success_rate": round(sum(1 for s in sub if s.ok) / len(sub), 4),
            "p95_ms": percentile([s.latency_ms for s in sub if s.ok], 95),
        }
    counts: dict[str, int] = {}
    for s in samples:
        key = str(s.status) if s.status else (s.error or "error")
        counts[key] = counts.get(key, 0) + 1
    return WindowStats(
        sample_count=len(samples),
        success_rate=round(len(ok) / len(samples), 4),
        p50_ms=percentile(lat, 50),
        p95_ms=percentile(lat, 95),
        p99_ms=percentile(lat, 99),
        by_operation=by_op,
        status_counts=counts,
    )


class SampleWindow:
    def __init__(self, maxlen: int = 50000):
        self._d: deque[Sample] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, s: Sample) -> None:
        with self._lock:
            self._d.append(s)

    def all(self) -> list[Sample]:
        with self._lock:
            return list(self._d)

    def between(self, t0: float, t1: float | None = None) -> list[Sample]:
        t1 = t1 if t1 is not None else time.time()
        return [s for s in self.all() if t0 <= s.ts <= t1]

    def last(self, n: int, after: float | None = None) -> list[Sample]:
        items = self.all()
        if after is not None:
            items = [s for s in items if s.ts >= after]
        return items[-n:]

    def stats_since(self, t0: float, t1: float | None = None) -> WindowStats:
        return stats(self.between(t0, t1))
