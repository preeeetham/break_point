# Experiment 2ad72f695676 — network_loss on mongodb

Status **completed** · planned by llm · 2026-10-10T09:39:04+00:00 → 2026-10-10T09:40:15+00:00

**Why:** mongodb has the highest criticality (9.5) yet no compose depends_on points at it; the env-var scan shows four services need it. It is protected, so a short degrade-only packet-loss fault is the safest way to test that hidden dependency first.

**Hypothesis:** Dropping 15% of packets leaving mongodb for 20 s will slow reads and writes through every Ditto service but keep the API error rate under 10%, and the API will be back to baseline latency within 30 s of rollback with no intervention.

**Result:** held — error rate 1% vs <=10%; unexpected services none; recovery 18.4s vs <=45s

| Window | Requests | Success | p95 ms |
|---|---|---|---|
| before | 80 | 100.0% | 41.2 |
| during | 72 | 98.6% | 1238.7 |
| after | 32 | 100.0% | 44.3 |

## Recovery

- recovered: True, time to recover 18.4s, outage seen by clients 2.1s
- Docker auto-restart: None
- remediation actions: none
- data integrity: True (stored seq=212, last acknowledged write seq=212)

## Health signals during the fault

| Service | checks | /alive OK | /ready OK | docker unhealthy | container down |
|---|---|---|---|---|---|
| mongodb | 7 | 0.71 | None | False | False |
| policies | 7 | 1.0 | 1.0 | False | False |
| things | 7 | 1.0 | 1.0 | False | False |
| things-search | 7 | 1.0 | 1.0 | False | False |
| connectivity | 7 | 1.0 | 1.0 | False | False |
| gateway | 7 | 1.0 | 1.0 | False | False |
| nginx | 7 | None | None | False | False |

## Findings

- **info** `api_impact` — API success dropped to 99% during the fault. 72 requests, status counts {'200': 53, '204': 18, 'timeout': 1}, p95 1238.7 ms (baseline 41.2 ms)
- **info** `auto_recovered` — Recovered on its own in 18s. outage as seen by clients: 2.1s

## Safety

- aborted early: False (None), watchdog fired: False, operator stop: False

## Timeline

- +   0.0s  planned  network_loss on mongodb (llm): protected datastore, degrade-only fault
- +   1.6s  injected  pumba netem loss 15% on bosch-ditto-mongodb-1 for 20s
- +   6.9s  first_api_error  status counts {'200': 14, '204': 5, 'timeout': 1}
- +  21.7s  injection_ended  fault expired on its own
- +  21.8s  rollback_started  
- +  22.4s  rollback_done  fault removed
- +  41.2s  recovered  success 100%, p95 44.3 ms, 0 remediation actions
- +  41.3s  integrity_check  stored seq=212, last acknowledged write seq=212

## Reproduce by hand

```sh
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock gaiaadm/pumba:latest netem --duration 20s --tc-image ghcr.io/alexei-led/pumba-alpine-nettools:latest loss --percent 15 bosch-ditto-mongodb-1
```
