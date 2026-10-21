# Human-approved fixes: 60-second demo

Audience: hackathon judges. Goal: show, in one minute, that Faultline can fix what it found, but only the exact change a person approves, and that it proves the fix by re-running the experiment or puts everything back.

The feature is documented in [docs/REFERENCE.md](../REFERENCE.md#human-approved-fixes). The full six-minute runbook is [docs/DEMO.md](../DEMO.md); this script slots in after its Step 5.

## The one thing to know before you start

Applying a fix and verifying it takes minutes, not seconds. The touched Ditto service is recreated (60-120 s to start and rejoin the cluster), Faultline waits for the stack to be healthy (up to 180 s), then re-runs the whole experiment (probe warm-up, the fault, recovery, a 20 s soak). So the 60 seconds below **approve one proposal live** and **show the before/after of the same kind of fix you approved during prep**. Say so on stage; it is true and it is the honest version.

For a recorded video you can instead approve live, cut, and come back to the same card once it has finished.

## Setup

| Window | What | Started |
|---|---|---|
| Terminal 1 | `faultline dashboard` | before demo |
| Terminal 2 | `faultline guardian` | before demo |
| Terminal 3 | idle, with the demo commands below already typed into its history | before demo |
| Browser | Control room, http://127.0.0.1:8765, scrolled so the Experiments table, the Scorecard and the Fix approvals panel are one scroll apart | before demo |

## Prep checklist (T-30 min)

Run from the repo root with the virtualenv active.

1. **Stack healthy.**
   ```bash
   faultline heal --restart     # only if anything is left over from earlier runs
   faultline resume             # no stale reports/STOP
   faultline status             # every container running, no active experiment
   ```
2. **Guardian running.** Terminal 2: `faultline guardian`. The control room's Guardian pill must be green. During an apply the Guardian deliberately leaves the services being recreated alone until the fix's deadline.
3. **Probes armed and a baseline.** In the control room press **Arm probes**, then **Capture baseline** (or `faultline baseline`).
4. **An experiment with a finding.** The reliable one on Ditto is a crash of `things`, which makes its neighbours keep answering `/ready` = 200 while the API fails:
   ```bash
   faultline run --target things --failure container_crash --param duration_s=30 --analyze
   ```
   It is usually aborted by the error-rate limit. That is fine: a re-run aborted by the same safety limit is still compared like for like. `--analyze` builds the proposals at the end; look for `[propose] Can I fix this? fx-...` lines. Note the experiment id (`<EID>` below).
5. **Dry run one fix, start to finish.** This gives you a finished card with real before/after numbers to show, and tells you how long the apply takes on this machine.
   ```bash
   faultline proposals                                  # three add_or_tighten_healthcheck proposals, one per service
   faultline approve <ID-A> --yes --reason "dry run"    # applies, re-runs, verifies or rolls back; takes minutes
   faultline scorecard                                  # refresh: the scorecard written during the apply still says "applied"
   ```
6. **Make the live proposal fresh.** The other proposals from step 4 were built against the override as it was before step 5, so applying one now would be refused ("the change differs from the one approved"). Reject them and propose again, with a long enough deadline for the demo:
   ```bash
   faultline reject <ID-B> --reason "stale after the dry run"
   faultline reject <ID-C> --reason "stale after the dry run"
   FIX_PROPOSAL_TIMEOUT_SECONDS=7200 faultline propose <EID> --no-llm
   faultline proposals                                  # one or two pending proposals, two hours left
   ```
   Pick one as `<ID-LIVE>` and note the first 12 characters of its hash (`<HASH12>`). If step 5 ended rolled back, the dry-run proposal for that service is proposed again too; pick a different service for the live one so the "Recently decided" card stays the dry run.
7. **Check the screen.** The Fix approvals panel shows `<ID-LIVE>` under "Waiting for a decision" and `<ID-A>` under "Recently decided". The probe pill is counting, the Guardian pill is green, no job is running.

## The script

| Time | Step | Exactly what to do | What to say |
|---|---|---|---|
| 0:00-0:10 | The finding | Control room, **Experiments** table: expand the `<EID>` row and point at `health_check_masked_failure` ("`things-search` kept reporting /ready=200 while the API was failing"). Glance at the **Resilience scorecard**: `things` is graded low | "We crashed `things`. Its neighbours kept answering 'ready' while every user request failed. That is Ditto issue 1865." |
| 0:10-0:22 | "Can I fix this?" | Scroll to **Fix approvals**. On the `<ID-LIVE>` card, read the five headings: 1. What is wrong, 2. Exactly what will change (the `+` and `-` lines: `/alive` becomes `/ready`), 3. Expected improvement, 4. Risks, 5. Rollback | "Faultline asks before it touches anything. The change is a diff generated by code from a fixed catalog; the model can only reword the explanation." |
| 0:22-0:28 | Same request, in the terminal | Terminal 3: `faultline proposals --id <ID-LIVE>`. Point at `Change hash (sha256): ...` and the `Approve:` line | "Same request on the CLI. The approval has to quote this hash." |
| 0:28-0:38 | Approve | Back in the browser, type a reason ("judges' demo") and click **Approve change `<HASH12>`**. Point out that the hash on the button is the one under the diff. The card moves to "Approved and in progress" with "Applying now" | "If the change had been edited after I read it, the hash would not match and nothing would be applied. Now it writes its own override file, never Ditto's compose file, recreates that service and re-runs the experiment. That takes minutes, so here is the same kind of fix, on the next service, that I approved before you came in." |
| 0:38-0:50 | Verified, before and after | Open **Recently decided**, then the `<ID-A>` card: status "Applied and verified", and the **Verification: before and after** table (time to recover, error rate, p95, service score, the targeted finding, each with before, after and a verdict; error rate is marked targeted). Read two numbers out | "Same experiment, before and after. Faultline keeps the change only if the targeted metric is no worse, no new finding appeared, and the finding it targeted did not get worse." |
| 0:50-0:60 | On the scorecard | Terminal 3: `sed -n '/^## Fix states/,/^Score/p' reports/scorecard.md` (the row for the service shows `verified`), then `faultline narrate \| grep "Fix verified"` | "And it lands on the scorecard and in the narrative: verified, by whom, with the numbers. The model proposes, code disposes, and a human decides." |

The control room's scorecard panel shows grades but does not draw fix states yet, which is why the last step uses `reports/scorecard.md` and the narrator.

To show the same approval from the terminal instead of the button (for example on a projector with no browser), the 0:28 step is:

```bash
faultline approve <ID-LIVE> --hash <HASH12> --reason "judges' demo"
```

It prints the proposal, approves it and keeps going with the apply in that terminal; cut away after the `approved ... (hash ...)` line.

## Fallback: the fix was rolled back

If the dry run (or the live one) ends **Rolled back** or **Verification failed**, show that instead. It is the safety story, and it is just as valid.

| Time | Exactly what to do | What to say |
|---|---|---|
| 0:38-0:50 | Open the card under "Recently decided": status "Rolled back", the verification table with the `worse` verdict, and the reason (for example `the re-run raised new finding(s): ...`). In the terminal, `faultline proposals --id <ID-A>` and point at the last audit lines: `applied -> verification_failed` and `verification_failed -> rolled_back` with "override restored and services recreated" | "It did not help, so Faultline put it back by itself: the override is restored byte for byte from its backup and the service recreated. Nobody had to remember to undo it." |
| 0:50-0:60 | `ls deploy/docker-compose.override.yml` (if this was the first fix, the file is gone again), then `sed -n '/^## Fix states/,/^Score/p' reports/scorecard.md` showing `rolled back (...)` with its reason | "The scorecard records that too. A fix that does not prove itself does not stay." |

If the apply never started (the card shows "The apply did not go ahead: ..."), read the refusal out loud: STOP latched, the probes not warm yet, a service not running, or another fix in flight. Each one is a preflight rule doing its job, and the proposal is still approved and can be applied with **Apply now** once the reason is gone.

## After the demo

```bash
faultline proposals --all        # everything that happened, with states
faultline resume                 # if anyone pressed STOP
```

Verified fixes stay applied in `deploy/docker-compose.override.yml` after the demo. The backup taken before the first fix, under `reports/remediation/backups/`, is the pre-fix state (a "did not exist" marker if there was no override before). Note that `scripts/up.sh` runs `docker compose up` inside `deploy/`, where compose loads the override automatically, so on the bundled stack the fixes survive a restart.
