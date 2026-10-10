# Progress — Weakness Scout

Build log for the Weakness Scout agent on branch `feature/weakness-scout`. The commits below built
it, on top of `7c984dd`; the concurrent test work has since added more (see **What is left**). The
feature is described for readers in
[README.md](README.md#predicting-weaknesses-before-breaking-anything); this file is the record of how
it was built, what was verified and how, and what is not done.

## What it is, in one paragraph

Every other agent in Faultline is empirical: the Risk agent scores an experiment that already ran,
the Fix agent recommends off a finding that was already observed. Correct, and it means Faultline has
nothing to say about a service nobody has gotten around to breaking — which matters on a hackathon
time budget and matters more the moment Faultline is pointed at an unfamiliar repo. The Scout runs
once, early (right after Topology, before any experiment), reads the compose file, the declared health
checks, the resource limits, the restart policies and the knowledge file, and produces a ranked list
of *predicted* weaknesses. `faultline scout --reconcile` later scores those predictions against the
experiments that have since run.

Two rules hold it together. The checks are 100 % deterministic — the LLM never decides whether a
weakness exists, because a hallucinated "no health check" on a service that has one would be
indistinguishable from a real finding in a severity-ranked report. And a prediction is labelled as one
everywhere: `PREDICTED_LABEL` = `"[predicted, unconfirmed]"` rides on the report, on every record, in
the Markdown header and on every CLI line, the same discipline the Fix agent applies to its
`[LLM suggestion, unverified]` extras.

## Build order

1. **The seven deterministic checks** (`faultline/agents/scout.py`): `check_health_checks` (two
   categories, `shallow_health_check` and `missing_health_check`), `check_redundancy`,
   `check_resource_limits`, `check_restart_policy`, `check_retry_backoff`, `check_published_ports`,
   `check_rate_limiting` — eight categories from seven check functions. Every finding quotes the
   compose field it was read from, or names precisely what is absent.
2. **Severity scoring**, in the Risk agent's additive style: 3.5 x relative criticality + category
   weight + 2 x blast fraction + 0.5 per other check the same service fails (max 3) + 1 if stateful
   + 0.5 if protected, clamped to 10, levels at the Risk agent's own 3 / 5.5 / 8 thresholds. A
   partially-present gap scales its category weight, so a service that sets a memory limit but no CPU
   limit does not score as if it set neither. Each finding also nominates the existing catalog failure
   type that would test it, resolved against `applicable()` and the safety policy so the suggestion is
   always runnable — MongoDB is protected and degrade-only, so its predictions ask for `network_delay`.
3. **Schema and blackboard slots** (`faultline/schemas.py`, `faultline/reporting/store.py`):
   `PredictedWeakness`, `SystemicPattern`, `WeaknessReport`, `WeaknessOutcome`,
   `WeaknessReconciliation`, `SuggestedExperiment`, `PREDICTED_LABEL`, plus `write_weaknesses` /
   `weaknesses` / `write_reconciliation` / `reconciliation` on the store.
4. **The LLM pass**, deliberately narrow: the one-sentence "why this matters" per finding, and at most
   two systemic-pattern narratives over findings the checks already made. The model cannot add to,
   remove from, re-rank or re-score the list; anything it returns for an id the checks did not produce
   is dropped, and a pattern citing fewer than two known ids is dropped.
5. **CLI** (`cmd_scout` and the `scout` subparser in `faultline/cli.py`): `faultline scout`,
   `--reconcile`, `--json`, `--no-llm`, registered after `topology` and before `baseline`, and added
   to `faultline demo` as step 2 of 7, with the reconciliation as step 5. With no `topology.json`
   on disk it maps one first with
   `live=False`, because static analysis has to work with the stack down.
6. **Planner integration** (`WEAKNESS_WEIGHT`, `WEAKNESS_EXACT_MATCH_BONUS` in
   `faultline/planning/planner.py`): strictly additive, bounded at +1.5.
7. **Reconcile** (`faultline/agents/reconcile.py`): confirmed / cleared / untested per prediction,
   pure arithmetic over `reports/`, no LLM anywhere in the module.

The commit order is not quite the logical order above: the schema landed first so the contract existed
before its producers, and reconcile landed before the CLI and planner wiring that consume it.

## The commits

| Commit | Purpose |
|---|---|
| `16743fa` | Schema + blackboard slots for predicted weaknesses — the contract, kept deliberately separate from the Risk agent's post-hoc measured severity |
| `6584d3c` | The seven deterministic checks, the severity formula and the first LLM prose pass. **This is the commit the Ditto sanity check belongs to** (below) |
| `b97495e` | `--reconcile`: the whole-system analogue of per-experiment `hypothesis_result.held`, plus the two gates that keep the headline number honest |
| `edbad8d` | `faultline scout` and `faultline scout --reconcile` on the CLI, following the existing command pattern, with every printed severity labelled as predicted |
| `a7a2ece` | Planner takes predicted weaknesses as an *additional* ranking signal, bounded at +1.5; the one change to already-working shared code, and strictly additive |
| `0dc3ed4` | Split the LLM pass into small, independently-failing calls after a real provider exposed the one-big-call design as both over-budget and all-or-nothing |
| `a73e263` | `tests/test_scout.py`, plus two fixes the tests and a clean-config run exposed: a well-configured deployment yields zero predictions, and a 0.0.0.0-bound port outranks a loopback one |
| `1cba35c` | Attribute a per-service finding to the prediction it is actually about, and honour the module's own "never raises" contract. Moved the real result from 6 confirmed to 8 |
| `78edf1a` | Say so when the safety policy forces a weaker stand-in experiment, instead of printing "take the single instance away" beside a latency injection |
| _(demo)_ | `faultline demo` runs the reconciliation as step 5 of 7, closing the loop it opens at step 2 |

## The Ditto sanity check — step 1, before any LLM or reconcile logic

This is the result the whole feature rests on, and it was taken before the LLM pass was wired in and
long before reconcile existed: `faultline scout` against `deploy/docker-compose.yml`, no model in the
loop, checking whether purely deterministic checks rediscover gaps whose correct answers are already
known elsewhere in this repository. **21 predictions.** Top of the ranking:

```
CRITICAL 9.5  no_redundancy        mongodb
HIGH     7.7  no_resource_limits   mongodb
HIGH     7.3  shallow_health_check policies
HIGH     7.0  no_redundancy        policies
HIGH     6.6  shallow_health_check things
HIGH     6.4  no_redundancy        things
HIGH     6.3  no_retry_or_backoff  policies,things,things-search,connectivity
MEDIUM   3.5  missing_health_check nginx
MEDIUM   3.0  no_rate_limiting     nginx
```

Why that is the right answer:

- The top prediction is MongoDB having no replica set, whose fix is already in the Fix agent's
  knowledge library word for word — "Run MongoDB as a replica set so the Ditto driver can fail over"
  (`faultline/agents/knowledge.py`) — except that the library only offers it *after* an experiment
  proves the point. The Scout predicts it from the compose file before anything is injected.
- `no_redundancy` on `policies` is that library's other Ditto-specific one, "run ≥2 policies
  instances".
- `shallow_health_check` fires on all five Ditto Java services off the real evidence
  ``test: curl --fail `hostname`:7626/alive`` — a bare liveness probe that never touches `mongodb`.
  That is the static shape of `health_check_masked_failure`, the finding the README documents against
  Ditto issue #1865, which until now Faultline could only find empirically.
- `weak_restart_policy` reports **zero** predictions, and that is the point: every Ditto service
  declares `restart: always`, which is part of why its crash recovery works cleanly. A check that
  fires on everything is a broken check, so the checks discriminate.
- The two lowest findings are `grafana` and `prometheus` publishing their own ports. Both are tagged
  outside the system under test, and both say so in their policy note rather than pretending to be
  testable.

## Predicted before, confirmed after

`faultline scout --reconcile` against the six experiment reports already on the blackboard:
**21 predicted, 8 confirmed, 0 cleared, 13 untested**, across 6 experiments considered.

The best single line of evidence: `shallow_health_check` on `things-search`, `connectivity` and
`gateway` was predicted from the compose file,
and experiment `fd1b18f67001` (`container_crash` on `things`) then raised a live
`health_check_masked_failure` — `/ready` OK for 100 % of checks while API success was 0 %, citing Ditto
issue #1865. Others: `no_resource_limits:mongodb` confirmed by `db_flood` (`7ec64f7a1498`) raising
`slow_recovery`, 121 s against the hypothesis' 60 s; `no_retry_or_backoff` confirmed by
`container_pause` (`2ee196bc306c`) turning requests into timeouts rather than errors.

Two gates keep that number honest, because an inflated hit rate would be worse than no hit rate:

1. **An `info`-severity finding is not a confirmation.** The injector writes `api_impact` at `info`
   precisely to record "API unaffected"; `auto_recovered` and an operator-stop `safety_abort` are
   always `info` too. Counting them would manufacture confirmations out of experiments that went fine.
2. **Each prediction declares `confirming_failure_types`**, so only relevant experiments bear on it.
   Before this gate a `container_crash` was "confirming" a predicted missing CPU limit through the
   generic `api_impact` code — a crash is a fast, clean error, which is exactly not evidence about an
   unbounded cgroup. Adding the gate dropped a spurious confirmation.

0 cleared is not an unimplemented state. Clearing needs a bearing experiment that ran the distance and
raised neither a confirming finding nor anything high or critical; on this blackboard every prediction
an experiment bore on produced a confirming finding instead, and the other 15 had nothing bear on them
at all.

## Verified, and how

- **Deterministic checks.** The Ditto sanity check above, run with `--no-llm`, against a compose file
  whose correct answers are independently recorded in `faultline/agents/knowledge.py` and in the
  README's own `health_check_masked_failure` / Ditto #1865 documentation. The silent
  `weak_restart_policy` check is part of the result, not an omission from it.
- **Reconcile.** Run against the six real experiment reports on the blackboard, not fixtures:
  21 / 6 / 0 / 15, output in `reports/weaknesses_reconcile.json` and `.md`. Both gates were added in
  response to a real spurious confirmation, not speculatively.
- **Planner integration.** Measured on the real data by building the candidate list with and without
  `reports/weaknesses.json`: `policies/container_stop` moves from rank #4 (priority 7.8) to rank #2
  (9.03), because `container_stop` is the experiment `no_redundancy:policies` nominates. With no
  weaknesses file on disk the ranking is unchanged, which is what the defaulted trailing argument
  buys. `tests/test_planner.py` gained 6 cases, including a no-regression test pinned to hand-derived
  numbers rather than a post-hoc snapshot, the headline ranking requirement, exact-match scoping, and
  the +1.5 bound.
- **Reconcile unit tests.** `tests/test_reconcile.py`, 315 lines, landed with `b97495e`.
- **LLM path, against a real provider** (Groq, `openai/gpt-oss-120b`): 21/21 explanations and 2
  systemic patterns, one of them the six core services sharing the same unbounded-cgroup gap and the
  same traffic-spike exposure as one story rather than six separate line items. Both patterns carry
  `[LLM suggestion, unverified]` and cite only ids the deterministic checks produced.
- **The fallback was exercised for real, not simulated.** A genuine 429 and a genuine
  `json_validate_failed` both left all 21 deterministic findings intact with templated prose, and the
  report's notes say how many batches failed. That failure is what commit `0dc3ed4` is: one call
  carrying all 21 weaknesses was ~14k characters, over the per-minute token budget of the tier these
  models are actually served from, and all-or-nothing — a single 429 cost every explanation on the
  report. Prose now goes in chunks of 7 and the patterns in one small call of their own.

## What is left

- **Nothing in the build order.** All seven steps are committed, the suite is green (405 tests, 133
  of them for this agent), and a full `scout` -> experiments -> `scout --reconcile` cycle produces a
  sensible predicted-vs-confirmed comparison. `faultline demo` now runs the reconciliation as step 5
  of 7, so the loop the scout opens is closed inside the demo itself.
- **Two commits carry pre-existing uncommitted work that was in the tree when this branch was cut.**
  `main`'s working tree held roughly fifteen modified or untracked files from other in-flight work
  (the repo-onboarding agent, the dashboard rewrite) at the time of branching, so `16743fa` also
  contains the onboarding `RepoKnowledge` / `OnboardingReport` schema block and `edbad8d` also
  contains `cmd_onboard` plus some `cmd_setup` / `cmd_heal` / `cmd_status` changes. Nothing was lost,
  but the attribution is wrong and is worth untangling before merge if that work lands separately.
- **Dashboard integration is built and verified end to end, but is not committed on this branch.** It
  is interleaved with a large pre-existing uncommitted dashboard rewrite in the working tree, so
  committing it here would drag that rewrite along with it. It works; it is simply not in these commits.
- **Nothing has been pushed, because there is no git remote configured** (`git remote -v` is empty).
  The commits exist only on this machine, on `feature/weakness-scout`. A PR body is
  written and ready; opening it needs a remote.
