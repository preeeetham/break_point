"""Agent 8, the Verifier: confirms recovery on the signals that raised the incident (REQUIREMENTS F6.2, F6.3).

After a settle period it re-evaluates the Sentinel's rules with a short rate window, until none of the incident's
(rule, service) signals fire for `verify_consecutive` checks in a row, or the window ends. Not recovered: roll back
every reversible action through the Action API, then escalate. A dry run changes nothing, so it is not verified.
"""
from __future__ import annotations

import time
from typing import Optional

from .. import notify
from ..actions.client import ActionAPIError
from ..models import ActionResult, Incident, iso
from .context import Context
from .sentinel import RULE_LABEL, evaluate


class Verifier:
    def __init__(self, ctx: Context, learned: Optional[dict] = None):
        self.ctx = ctx
        self.learned = learned if learned is not None else {}

    def run(self, inc: Incident) -> Incident:
        v = inc.verification
        executed = [r for r in inc.execution.results if r.ok and not r.dry_run]
        if not executed:
            v.checked, v.recovered = False, None
            v.notes = ("dry run: the Action API previewed the plan, nothing changed, so there is no recovery to verify"
                       if inc.execution.results and all(r.dry_run for r in inc.execution.results)
                       else "no action succeeded; nothing to verify")
            return inc
        keys = {(a.rule, a.service) for a in inc.alerts}
        v.signals_checked = [f"{RULE_LABEL.get(r, r)} on {s}" for r, s in sorted(keys)]
        s = self.ctx.settings
        with self.ctx.status.working("verifier", inc.id, "checking recovery"):
            self._sleep(s.verify_settle_s)
            clear, deadline = 0, time.time() + s.verify_window_s
            window = "1m"
            while True:
                self.ctx.safety.check()
                signals, _ = evaluate(self.ctx, {k: x for k, x in self.learned.items()}, window=window)
                firing = sorted({f"{RULE_LABEL.get(sg.rule, sg.rule)} on {sg.service}: {sg.signal}"
                                 for sg in signals if sg.key in keys})
                v.samples.append({"at": iso(), "firing": firing})
                clear = 0 if firing else clear + 1
                self.ctx.bus.publish("verifier", "verify", ("still firing: " + "; ".join(firing)) if firing
                                     else f"signals clear ({clear}/{s.verify_consecutive})", incident_id=inc.id)
                if clear >= s.verify_consecutive:
                    v.checked, v.recovered = True, True
                    v.notes = f"all {len(keys)} triggering signals clear for {clear} consecutive checks"
                    return inc
                if time.time() >= deadline:
                    break
                self._sleep(min(s.sentinel_interval_s, max(deadline - time.time(), 0)))
        v.checked, v.recovered = True, False
        v.notes = "signals still firing at the end of the verification window: " + "; ".join(v.samples[-1]["firing"])
        self.rollback(inc)
        return inc

    def rollback(self, inc: Incident) -> None:
        for r in [r for r in inc.execution.results if r.ok and not r.dry_run and r.execution_id]:
            try:
                rb = self.ctx.actions.rollback(r.execution_id)
            except ActionAPIError as exc:
                rb = ActionResult(action="rollback", execution_id=r.execution_id, ok=False, dry_run=False,
                                  message=str(exc))
            inc.execution.rollback_results.append(rb)
            if rb.ok and "not reversible" not in rb.message:
                inc.execution.rollback_performed = True
            self.ctx.bus.publish("verifier", "action", f"rollback of {r.action}: {rb.message}", incident_id=inc.id,
                                 detail=rb.model_dump())
        inc.escalation_reason = "recovery not confirmed after the approved action; rolled back where possible"
        self.ctx.bus.publish("verifier", "step", notify.send(self.ctx.settings, f"[Incident Zero] ESCALATION {inc.id}",
                                                             f"{inc.title}\n\n{inc.verification.notes}"),
                             incident_id=inc.id)

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.ctx.safety.check()
            time.sleep(min(1.0, max(end - time.time(), 0)))
