# Experiment f435aa8cd6f5 — network_delay on gateway

Status **completed** · planned by deterministic · 2026-10-10T10:00:04+00:00 → 2026-10-10T10:01:07+00:00

**Why:** gateway is the only path from nginx into the cluster and has not been tested with a slow network; ranked first among untested degrade-only candidates.

**Hypothesis:** Adding 400 +/- 100 ms of latency on gateway for 30 s will raise p95 latency well above baseline without failing requests, and recover within 15 s of rollback.

**Result:** held — error rate 0% vs <=5%; unexpected services none; recovery 6.2s vs <=22s

| Window | Requests | Success | p95 ms |
|---|---|---|---|
| before | 80 | 100.0% | 41.2 |
| during | 112 | 100.0% | 788.5 |
| after | 32 | 100.0% | 42.8 |

## Recovery

- recovered: True, time to recover 6.2s, outage seen by clients 0.0s
- Docker auto-restart: None
- remediation actions: none
- data integrity: True (stored seq=377, last acknowledged write seq=377)

## Health signals during the fault

| Service | checks | /alive OK | /ready OK | docker unhealthy | container down |
|---|---|---|---|---|---|
| mongodb | 10 | 1.0 | None | False | False |
| policies | 10 | 1.0 | 1.0 | False | False |
| things | 10 | 1.0 | 1.0 | False | False |
| things-search | 10 | 1.0 | 1.0 | False | False |
| connectivity | 10 | 1.0 | 1.0 | False | False |
| gateway | 10 | 1.0 | 1.0 | False | False |
| nginx | 10 | None | None | False | False |

## Findings

- **low** `api_impact` — API stayed available (100%) but p95 latency rose 41.2 -> 788.5 ms. 112 requests, status counts {'200': 84, '204': 28}, p95 788.5 ms (baseline 41.2 ms)
- **info** `auto_recovered` — Recovered on its own in 6s. outage as seen by clients: 0.0s

## Safety

- aborted early: False (None), watchdog fired: False, operator stop: False

## Timeline

- +   0.0s  planned  network_delay on gateway (deterministic)
- +   1.4s  injected  pumba netem delay 400ms +/- 100ms on bosch-ditto-gateway-1 for 30s
- +  31.5s  injection_ended  fault expired on its own
- +  31.6s  rollback_started  
- +  32.2s  rollback_done  fault removed
- +  38.4s  recovered  success 100%, p95 42.8 ms, 0 remediation actions
- +  38.5s  integrity_check  stored seq=377, last acknowledged write seq=377

## Reproduce by hand

```sh
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock gaiaadm/pumba:latest netem --duration 30s --tc-image ghcr.io/alexei-led/pumba-alpine-nettools:latest delay --time 400 --jitter 100 bosch-ditto-gateway-1
```
