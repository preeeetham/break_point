# Target: Eclipse Ditto

[Eclipse Ditto](https://github.com/eclipse-ditto/ditto) 3.9, an open-source IoT digital-twin platform,
under Docker Compose: MongoDB 7, the five Ditto services (policies, things, things-search, connectivity,
gateway) on one Pekko cluster, the Ditto UI, nginx in front with basic auth, and a Prometheus / cAdvisor
/ Blackbox / Grafana observability stack. This was Break Point's first target; everything that used to be
hardcoded for it lives here now.

```bash
echo BREAK_POINT_TARGET=ditto >> .env
scripts/up.sh            # cold start ~2 min; waits for /health = UP
break_point setup
break_point dashboard
```

| File | What it holds |
|---|---|
| `target.yaml` | compose file, project `break_point`, `http://127.0.0.1:8080`, sandbox credentials `ditto` / `ditto`, links |
| `ditto_knowledge.yaml` | hand-written from Ditto's architecture pages: the Pekko cluster, the nine documented edges, health port 7626 with `/alive` and `/ready`, outage error rates for things-search and connectivity |
| `probe.yaml` | the Things-API traffic: a canary thing, reads, sequence-numbered property writes, searches |
| `rules.yaml` | the 18 Ditto-specific fix rules (`mode: replace`): replica sets, Pekko split-brain, the policy cache, issue 1865 and discussion 1861, nginx upstreams |
| `thresholds.yaml` | `mongodb` is protected: degrade-only |
| `deploy/` | the sandbox: compose file, nginx, Prometheus, Blackbox, Grafana dashboard |
| `fable/` | the stage-by-stage React front end, served at `/fable/` when this target is selected; its Vercel project's root directory is `targets/ditto/fable` |
| `reports-demo/` | a captured blackboard you can browse without Docker (`scripts/seed_demo.py` regenerates it) |
| `scripts/` | `seed_demo.py` (the blackboard above) and `demo_doctor.py` (pre-demo readiness check) |
| `tests/` | the real-file regression tests: the target reproduces the pre-split behaviour exactly; `tests/integration` is the live suite (`BREAK_POINT_LIVE=1`) |
| `docs/` | the long-form guide, the full reference, the demo runbook, the Day-2 findings, the pitch deck and the project history |

The documents in `docs/` were written before the core/target split. Where they name `deploy/`,
`config/ditto_knowledge.yaml`, `reports-demo/`, `fable/` or `DITTO_URL` / `DITTO_USER` / `DITTO_PASSWORD`,
read `targets/ditto/deploy/`, `targets/ditto/ditto_knowledge.yaml`, `targets/ditto/reports-demo/`,
`targets/ditto/fable/` and `TARGET_URL` / `TARGET_USER` / `TARGET_PASSWORD`.

**Attaching to an upstream Ditto stack** instead of the sandbox: set `BREAK_POINT_COMPOSE_PROJECT` to its
project name (e.g. `bosch-ditto`) and `TARGET_URL` / `TARGET_USER` / `TARGET_PASSWORD` in `.env`; if its
compose files need variables its own launcher exports, list them in `BREAK_POINT_COMPOSE_ENV`. Changing
`DITTO_PORT` in `deploy/.env` moves nginx; set `TARGET_URL` to match.
