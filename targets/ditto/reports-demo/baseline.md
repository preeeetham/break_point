# Steady-state baseline

Captured 2026-10-10T09:31:04+00:00 against http://127.0.0.1:8080, 60.0s.

API: 240 requests, success 100.00%, p50 17.9 ms, p95 41.2 ms, p99 98.3 ms.

| Operation | Count | Success | p95 ms |
|---|---|---|---|
| get_thing | 90 | 100.0% | 35.02 |
| put_property | 60 | 100.0% | 37.08 |
| search | 60 | 100.0% | 45.32 |
| get_property | 30 | 100.0% | 24.72 |

| Service | Health availability |
|---|---|
| mongodb | 100% |
| policies | 100% |
| things | 100% |
| things-search | 100% |
| connectivity | 100% |
| gateway | 100% |
| nginx | - |

## Safety thresholds derived for injection

- Abort when trailing error rate > 30% (last 10 requests)
- Abort when p95 latency > 3000 ms
- Abort when a service outside the hypothesis fails 3 health checks in a row
- Hard cap 90s per experiment (+15s watchdog)
- Recovered when success >= 95% and p95 < 300 ms for 3 consecutive checks

## Notes

- none
