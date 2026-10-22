# Experiment f63b02a3c8ac — container_pause on things

Status **aborted** · planned by deterministic · 2026-10-10T10:21:04+00:00 → 2026-10-10T10:22:02+00:00

**Why:** things owns every digital twin and has never been tested with a hung process; a SIGSTOP keeps TCP connections open, so callers hang instead of failing fast.

**Hypothesis:** Freezing things for 20 s will fail thing reads and writes behind gateway; Ditto's health checks will flag the outage, and the API will recover within 15 s of unpause.

**Result:** violated — error rate 100% vs <=100%; unexpected services none; recovery 26.0s vs <=22s

| Window | Requests | Success | p95 ms |
|---|---|---|---|
| before | 80 | 100.0% | 41.2 |
| during | 24 | 0.0% | None |
| after | 32 | 100.0% | 47.9 |

## Recovery

- recovered: True, time to recover 26.0s, outage seen by clients 33.2s
- Docker auto-restart: False
- remediation actions: none
- data integrity: True (stored seq=431, last acknowledged write seq=431)

## Health signals during the fault

| Service | checks | /alive OK | /ready OK | docker unhealthy | container down |
|---|---|---|---|---|---|
| mongodb | 3 | 1.0 | None | False | False |
| policies | 3 | 1.0 | 1.0 | False | False |
| things | 3 | None | None | False | False |
| things-search | 3 | 1.0 | 1.0 | False | False |
| connectivity | 3 | 1.0 | 1.0 | False | False |
| gateway | 3 | 1.0 | 1.0 | False | False |
| nginx | 3 | None | None | False | False |

## Findings

- **high** `api_impact` — API success dropped to 0% during the fault. 24 requests, status counts {'timeout': 24}, p95 None ms (baseline 41.2 ms)
- **high** `health_check_masked_failure` — things-search kept reporting /ready=200 while the API was failing. /ready OK 100% of 3 checks while API success was 0%; cf. Ditto issue #1865 (health checks ignore subsystem health)
- **high** `health_check_masked_failure` — connectivity kept reporting /ready=200 while the API was failing. /ready OK 100% of 3 checks while API success was 0%; cf. Ditto issue #1865 (health checks ignore subsystem health)
- **high** `health_check_masked_failure` — gateway kept reporting /ready=200 while the API was failing. /ready OK 100% of 3 checks while API success was 0%; cf. Ditto issue #1865 (health checks ignore subsystem health)
- **medium** `no_automated_remediation` — Nothing in the platform reacted to the outage. target container stayed 'running' and never became 'unhealthy' during the fault; compose `restart: always` only acts on exit, never on health
- **info** `auto_recovered` — Recovered on its own in 26s. outage as seen by clients: 33.2s
- **medium** `slow_recovery` — Recovery slower than hypothesised. 26s vs expected 15s
- **medium** `safety_abort` — Kill switch fired (error_rate). {"trailing_samples": 6, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3}

## Safety

- aborted early: True (error_rate), watchdog fired: False, operator stop: False

## Timeline

- +   0.0s  planned  container_pause on things (deterministic)
- +   0.6s  injected  SIGSTOP bosch-ditto-things-1 (docker pause)
- +   1.7s  target_condition_observed  docker reports paused/healthy
- +   2.4s  first_api_error  status counts {'timeout': 2}
- +   8.3s  safety_abort  error_rate {"trailing_samples": 6, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3}
- +   8.3s  rollback_started  
- +   8.5s  rollback_done  fault removed
- +  34.5s  recovered  success 100%, p95 47.9 ms, 0 remediation actions
- +  34.6s  integrity_check  stored seq=431, last acknowledged write seq=431

## Reproduce by hand

```sh
docker pause bosch-ditto-things-1
sleep 20
docker unpause bosch-ditto-things-1
```
