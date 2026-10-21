# Onboarding a target

A *target* is one application under test. Everything Faultline needs to know about it lives in one
directory, `targets/<name>/`, and nothing outside that directory names it. The core (`main`) ships with
no target; each one lives on its own branch, `target/<name>`, which is `main` plus that directory.

```
targets/<name>/
  target.yaml            settings for this target (required)
  <name>_knowledge.yaml  what compose cannot say (written by `faultline onboard`, then reviewed)
  probe.yaml             the synthetic traffic that stands for real users (recommended)
  thresholds.yaml        which services are protected (recommended once it has a datastore)
  rules.yaml             fix rules that name this application's services (optional)
  deploy/                the compose stack, if the target ships its own sandbox
  tests/                 tests that need this target's real files; `make test` collects them
  docs/                  anything else that belongs to it
```

Every optional file has a safe default when it is absent: health-URL-only probing, no protected
services (and a warning from `faultline setup` for each datastore), the generic fix rules.

## 1. Branch

```bash
git checkout main && git checkout -b target/<name>
```

Keep every change inside `targets/<name>/`. A target branch that only adds files merges `main` without
conflicts, so core fixes reach every target with `git merge main`.

## 2. Onboard the repository

```bash
faultline onboard owner/repo --out targets/<name>          # or a GitHub URL, or a local path
```

This clones the repository (shallow, cached under `.cache/onboarding/`), finds its compose file, README
and architecture docs, makes one bounded LLM pass for relationships only prose states, and writes:

- `targets/<name>/<repo>_knowledge.yaml` — documented edges, role overrides, the shared health port,
  the cluster alias, and each datastore's engine with the code evidence for it (the Dockerfile `FROM`
  line, or the client drivers its dependents declare). Engines are decided by code, never by the model.
- `targets/<name>/target.yaml` — a starting point that points at the repository's own compose file.
  Never overwritten once it exists.

Review the knowledge file before trusting it: documented edges change the criticality ranking, and a
role of `ui` or `other` takes a service out of testing altogether. `--no-llm` writes the structural part
only; `--dry-run` writes nothing.

## 3. target.yaml

Any `Settings` field, layered under `.env` and the environment. Relative paths are relative to the
target directory.

```yaml
app_name: Acme Shop                 # how prompts and reports name it
compose_file: deploy/docker-compose.yml
nginx_conf: deploy/nginx/nginx.conf # optional; adds proxy_pass edges
compose_project: acme               # the only Docker project Faultline may touch
target_url: http://127.0.0.1:8080   # the published entry point
target_user: probe                  # optional basic auth for the probe (secrets belong in .env)
thresholds_file: thresholds.yaml
links:                              # printed by `faultline target` and scripts/up.sh
  API: http://127.0.0.1:8080/api
```

Then select it: `FAULTLINE_TARGET=<name>` in `.env`, and check with `faultline target`.

## 4. probe.yaml

Which requests stand for real users is a product decision, so a human writes this file. Without it the
probe polls the health URL, and cascade attribution can only implicate the entry tier.

```yaml
health:
  path: /health
  expect: {status: UP}          # optional: values the health JSON must contain
setup:                          # sent once before probing
  - {name: create_canary, method: PUT, path: "/api/items/{namespace}:canary", json: {seq: 0}}
operations:
  read:  {method: GET, path: "/api/items/{namespace}:canary", route: [nginx, api, items]}
  write: {method: PUT, path: "/api/items/{namespace}:canary/seq", json: "{seq}", write: true,
          route: [nginx, api, items]}
  find:  {method: GET, path: /api/search, params: {q: canary}, route: [nginx, api, search]}
mix: [read, write, read, find]  # sent in this order, round robin, at probe_rps
integrity: read_seq             # optional: an operation whose JSON answer is the last stored seq
spike: read                     # what traffic_spike repeats
```

`{namespace}` becomes `probe_namespace`; a body that is exactly `"{seq}"` becomes the write's sequence
number. `route` lists the services a request passes through, entry first: it is how Faultline tells
whose traffic failed while their own health checks stayed green. Every name is checked at load.

## 5. Protect the datastores

Copy `config/thresholds.yaml` to `targets/<name>/thresholds.yaml`, set `thresholds_file:
thresholds.yaml` in `target.yaml`, and list what may only be degraded:

```yaml
policy:
  protected:
    db:
      reason: "sole datastore for every service; degrade-only, never a full outage"
      allowed_failure_types: [network_delay, network_loss, cpu_stress, disk_pressure, db_flood]
```

`faultline setup` warns about every datastore that is not protected.

## 6. Fix rules (optional)

The generic rules match failure types, finding codes and roles. Rules that name this application's own
services and settings go in `rules.yaml`, same fields as `faultline/agents/knowledge.py`:

```yaml
mode: extend              # or `replace`: these rules stand in for the generic library entirely
rules:
  - match: {targets: [orders], failure_types: [container_crash]}   # also: roles, finding_codes
    title: Run two orders instances behind the gateway
    category: architecture  # configuration | architecture | observability | operations | code
    effort: M
    confidence: 0.8
    rationale: orders is single-instance, so a crash is a full outage of checkout.
    steps: ["deploy.replicas: 2", "..."]
    verify: ["kill one: success stays above 90%"]
    references: []
```

## 7. Bring it up and map it

```bash
scripts/up.sh            # starts the compose stack, waits for the health URL
faultline setup          # Docker, health, protection, LLM
faultline topology       # check the map: roles, engines, edges, findings
faultline scout          # predictions, before anything is broken
```

The topology lists each datastore's engine and where it came from. A datastore whose engine is
unknown is a finding, not an error: it gets every generic fault, and no engine-specific command.

## Datastore engines

`faultline/topology/datastores.py` is the only place engine-specific commands live: a health ping, a
compose healthcheck and a write flood with its cleanup, per engine. MongoDB, PostgreSQL and Redis have
them; MySQL/MariaDB and Cassandra are recognised but have none yet. Adding one is a core change, not a
target change: run each command against the engine's official image first (including the flood being
killed mid-run by `chaos.actions.kill_marked_script`), record the image in `verified`, then add the
entry and a test in `tests/test_datastores.py`.
