# Experiment 6143d497b287 — container_stop on policies

Status **aborted** · planned by operator · 2026-10-10T10:43:04+00:00 → 2026-10-10T10:44:10+00:00

**Why:** operator-selected graceful stop (sigterm) on policies

**Hypothesis:** taking away policies (graceful stop (sigterm)) for 30s will push the synthetic API error rate to at most 100%, impact at most ['connectivity', 'gateway', 'nginx', 'things', 'things-search'], and the API will recover within 90s after rollback without manual intervention

**Result:** held — error rate 25% vs <=100%; unexpected services none; recovery 41.5s vs <=135s

| Window | Requests | Success | p95 ms |
|---|---|---|---|
| before | 80 | 100.0% | 41.2 |
| during | 16 | 75.0% | 214.0 |
| after | 32 | 100.0% | 45.0 |

## Recovery

- recovered: True, time to recover 41.5s, outage seen by clients 12.3s
- Docker auto-restart: False
- remediation actions: none
- data integrity: True (stored seq=502, last acknowledged write seq=502)

## Health signals during the fault

| Service | checks | /alive OK | /ready OK | docker unhealthy | container down |
|---|---|---|---|---|---|
| mongodb | 2 | 1.0 | None | False | False |
| policies | 2 | None | None | True | True |
| things | 2 | 1.0 | 1.0 | False | False |
| things-search | 2 | 1.0 | 1.0 | False | False |
| connectivity | 2 | 1.0 | 1.0 | False | False |
| gateway | 2 | 1.0 | 1.0 | False | False |
| nginx | 2 | None | None | False | False |

## Findings

- **medium** `api_impact` — API success dropped to 75% during the fault. 16 requests, status counts {'200': 9, '503': 4, '204': 3}, p95 214.0 ms (baseline 41.2 ms)
- **info** `auto_recovered` — Recovered on its own in 42s. outage as seen by clients: 12.3s
- **info** `safety_abort` — Kill switch fired (operator_stop). {}

## Safety

- aborted early: True (operator_stop), watchdog fired: False, operator stop: True

## Timeline

- +   0.0s  planned  container_stop on policies (operator): operator-selected graceful stop (sigterm) on policies
- +   1.1s  injected  docker stop bosch-ditto-policies-1
- +   2.3s  target_condition_observed  docker reports exited/None
- +   3.9s  first_api_error  status counts {'200': 5, '204': 2, '503': 1}
- +   5.2s  safety_abort  operator_stop {}
- +   5.2s  rollback_started  
- +   5.6s  rollback_done  fault removed
- +  47.0s  recovered  success 100%, p95 45.0 ms, 0 remediation actions
- +  47.1s  integrity_check  stored seq=502, last acknowledged write seq=502

## Reproduce by hand

```sh
docker stop bosch-ditto-policies-1
sleep 30
docker start bosch-ditto-policies-1
```
