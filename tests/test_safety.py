import time

from faultline.observe.window import Sample, SampleWindow
from faultline.safety.monitor import SafetyMonitor, Verdict, Watchdog
from faultline.safety.thresholds import thresholds_from


class FakeHealth:
    def __init__(self, failures: dict[str, int] | None = None):
        self.failures = failures or {}

    def consecutive_failures(self, svc: str) -> int:
        return self.failures.get(svc, 0)


def _window(ok: int, bad: int, latency: float = 20.0, t0: float = 1000.0) -> SampleWindow:
    w = SampleWindow()
    for i in range(ok):
        w.add(Sample(ts=t0 + i, op="get_thing", status=200, latency_ms=latency))
    for i in range(bad):
        w.add(Sample(ts=t0 + ok + i, op="get_thing", status=503, latency_ms=5.0))
    return w


def _monitor(thresholds, window, health=None, protected=None, stop=False, injected_at=999.0):
    return SafetyMonitor(thresholds, window, health or FakeHealth(), protected or [], lambda: stop, injected_at)


def test_thresholds_from_uses_max_of_floor_and_factor(policy_raw):
    low = thresholds_from(policy_raw, 60.0)
    assert low.abort_p95_ms_above == 3000.0  # floor wins over 5 x 60
    assert low.recovery_p95_ms_below == 300.0  # floor wins over 2 x 60
    assert low.baseline_p95_ms == 60.0
    high = thresholds_from(policy_raw, 1000.0)
    assert high.abort_p95_ms_above == 5000.0
    assert high.recovery_p95_ms_below == 2000.0
    none = thresholds_from(policy_raw, None)
    assert none.abort_p95_ms_above == 3000.0 and none.baseline_p95_ms is None
    assert low.trailing_samples == 10 and low.abort_error_rate_above == 0.3
    assert low.non_target_health_failures_in_a_row == 3


def test_no_abort_with_fewer_than_five_trailing_samples(thresholds):
    v = _monitor(thresholds, _window(0, 4))
    out = v.evaluate()
    assert isinstance(out, Verdict) and out.abort is False
    assert out.detail["trailing_samples"] == 4
    assert "error_rate" not in out.detail


def test_abort_on_error_rate(thresholds):
    out = _monitor(thresholds, _window(6, 4)).evaluate()
    assert out.abort and out.reason == "error_rate"
    assert out.detail["error_rate"] == 0.4 and out.detail["threshold"] == 0.3


def test_no_abort_when_error_rate_within_bounds(thresholds):
    out = _monitor(thresholds, _window(8, 2)).evaluate()
    assert out.abort is False and out.detail["error_rate"] == 0.2


def test_abort_on_latency(thresholds):
    out = _monitor(thresholds, _window(10, 0, latency=thresholds.abort_p95_ms_above + 1)).evaluate()
    assert out.abort and out.reason == "latency"
    assert out.detail["threshold_ms"] == thresholds.abort_p95_ms_above


def test_samples_before_injection_are_ignored(thresholds):
    # all the bad samples predate injected_at -> nothing trailing -> no abort
    w = _window(0, 10, t0=100.0)
    assert _monitor(thresholds, w, injected_at=500.0).evaluate().abort is False


def test_abort_on_blast_radius(thresholds):
    health = FakeHealth({"things": 3, "gateway": 1})
    out = _monitor(thresholds, _window(10, 0), health, protected=["gateway", "things"]).evaluate()
    assert out.abort and out.reason == "blast_radius:things"
    assert out.detail["consecutive_failures"] == 3
    ok = _monitor(thresholds, _window(10, 0), FakeHealth({"things": 2}), protected=["things"]).evaluate()
    assert ok.abort is False


def test_operator_stop_takes_precedence(thresholds):
    health = FakeHealth({"things": 5})
    out = _monitor(thresholds, _window(0, 10), health, protected=["things"], stop=True).evaluate()
    assert out.abort and out.reason == "operator_stop"


def test_watchdog_fires_after_delay():
    fired = []
    wd = Watchdog(0.05, lambda: fired.append(True))
    wd.arm()
    deadline = time.time() + 2.0
    while not wd.fired and time.time() < deadline:
        time.sleep(0.01)
    assert wd.fired and fired == [True]


def test_watchdog_disarm_prevents_firing():
    fired = []
    wd = Watchdog(0.05, lambda: fired.append(True))
    wd.arm()
    wd.disarm()
    time.sleep(0.15)
    assert wd.fired is False and fired == []


def test_watchdog_swallows_callback_exceptions():
    def boom():
        raise RuntimeError("nope")

    wd = Watchdog(0.01, boom)
    wd.arm()
    deadline = time.time() + 2.0
    while not wd.fired and time.time() < deadline:
        time.sleep(0.01)
    assert wd.fired
