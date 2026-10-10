from faultline.observe.window import Sample, SampleWindow, percentile, stats


def _s(ts: float, status: int = 200, latency: float = 10.0, op: str = "get_thing", error: str = "") -> Sample:
    return Sample(ts=ts, op=op, status=status, latency_ms=latency, error=error)


def test_percentile_edge_cases():
    assert percentile([], 95) is None
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 99) == 42.0
    # k = (4-1) * 0.5 = 1.5 -> halfway between 2 and 3
    assert percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_stats_empty_window():
    st = stats([])
    assert st.sample_count == 0 and st.success_rate is None and st.p95_ms is None


def test_stats_success_rate_latency_and_counts():
    samples = [_s(i, 200, 10 + i) for i in range(8)]
    samples += [_s(8, 503, 5.0), _s(9, 0, 5000.0, error="timeout")]
    st = stats(samples)
    assert st.sample_count == 10
    assert st.success_rate == 0.8
    # latency percentiles only consider successful samples -> the 5000 ms timeout is excluded
    assert st.p95_ms is not None and st.p95_ms < 20
    assert st.status_counts == {"200": 8, "503": 1, "timeout": 1}


def test_stats_by_operation():
    samples = [_s(0, 200, 10, "get_thing"), _s(1, 500, 10, "get_thing"), _s(2, 200, 30, "search")]
    st = stats(samples)
    assert set(st.by_operation) == {"get_thing", "search"}
    assert st.by_operation["get_thing"]["count"] == 2
    assert st.by_operation["get_thing"]["success_rate"] == 0.5
    assert st.by_operation["search"]["p95_ms"] == 30


def test_status_zero_without_error_text_counted_as_error():
    st = stats([_s(0, 0, 1.0)])
    assert st.status_counts == {"error": 1}


def test_sample_window_between_last_and_stats_since():
    w = SampleWindow()
    for i in range(10):
        w.add(_s(100.0 + i, 200 if i % 2 == 0 else 500, 10.0))
    assert [s.ts for s in w.between(103.0, 105.0)] == [103.0, 104.0, 105.0]
    assert len(w.between(100.0)) == 10  # open-ended upper bound uses now()
    assert [s.ts for s in w.last(3)] == [107.0, 108.0, 109.0]
    assert [s.ts for s in w.last(3, after=108.0)] == [108.0, 109.0]
    assert w.last(5, after=200.0) == []
    st = w.stats_since(105.0, 109.0)  # ts 105..109 -> even offsets (106, 108) are the successes
    assert st.sample_count == 5
    assert st.success_rate == 0.4
    assert st.status_counts == {"200": 2, "500": 3}
