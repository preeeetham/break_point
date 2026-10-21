# Resilience scorecard — bosch-ditto

_Generated 2026-10-10 11:01 UTC · 4 experiment(s) · overall **76.0/100**_

4 scored experiment(s) across 4 of 7 services; overall 76.0/100. Weakest: `things` (F, 25.7/100) — API success fell to 0%; health checks stayed green during failure; sat unhealthy with no automated remediation. 1 experiment(s) rated high or critical risk.

| # | Service | Grade | Score | Experiments | Worst success | Slowest recovery | Findings | Notes |
|---|---------|-------|-------|-------------|---------------|------------------|----------|-------|
| 1 | `things` | **F** | 25.7 | 1 | 0% | 26 s | 8 | health checks stayed green during failure; sat unhealthy with no automated remediation; slow recovery; tested: container_pause |
| 2 | `policies` | **B** | 83.1 | 1 | 75% | 42 s | 3 | tested: container_stop |
| 3 | `mongodb` | **A** | 96.4 | 1 | 99% | 18 s | 2 | tested: network_loss |
| 4 | `gateway` | **A** | 99.0 | 1 | 100% | 6 s | 2 | tested: network_delay |
| 5 | `connectivity` | **—** | — | 0 | — | — | 0 | not tested |
| 6 | `nginx` | **—** | — | 0 | — | — | 0 | not tested |
| 7 | `things-search` | **—** | — | 0 | — | — | 0 | not tested |

Score = 100 − mean penalty; penalty = 40·(1−success during) + 20·min(TTR/120 s, 1) + 20·(manual recovery) + 10·(high/critical findings, max 3) + 5·(cascade). Grades: A ≥ 90, B ≥ 80, C ≥ 65, D ≥ 50, else F.
