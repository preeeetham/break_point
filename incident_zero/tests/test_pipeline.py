"""End to end through every agent, on fake telemetry and an in-process Action API."""
from __future__ import annotations

import time
from typing import Callable, Optional

import pytest

from incident_zero.actions.mock_server import ActionAPI
from incident_zero.agents.approval import ApprovalError
from incident_zero.agents.context import Context
from incident_zero.llm import LLM
from incident_zero.models import ActionResult, Incident
from incident_zero.orchestrator import Orchestrator
from incident_zero.redact import mentions_flags
from incident_zero.report_schema import validate
from incident_zero.safety import Safety

from . import fakes


class Runtime:
    """Fake Action API runtime: actions mutate the fake system through `fixes`."""

    def __init__(self, svcs, fixes: Optional[dict[str, Callable]] = None):
        self.svcs, self.fixes, self.calls, self.undone = svcs, fixes or {}, [], []

    def services(self):
        return set(self.svcs)

    def _act(self, name, service, dry, undo=None):
        self.calls.append((name, service, dry))
        if dry:
            return f"would {name} {service}", None
        fn = self.fixes.get(name)
        if fn:
            fn(self.svcs, service)
        return f"did {name} {service}", undo

    def restart_service(self, service, dry):
        return self._act("restart_service", service, dry)

    def start_service(self, service, dry):
        return self._act("start_service", service, dry)

    def rollback_release(self, service, dry):
        return self._act("rollback_release", service, dry, {"kind": "flags", "prev": {}})

    def update_resources(self, service, dry, cpus=None, memory_mb=None):
        return self._act("update_resources", service, dry, {"kind": "resources", "service": service, "prev": {}})

    def clear_network_impairment(self, service, dry):
        return self._act("clear_network_impairment", service, dry)

    def reconnect_network(self, service, dry):
        return self._act("reconnect_network", service, dry)

    def undo(self, undo):
        self.undone.append(undo)
        return "restored"


class InProcClient:
    def __init__(self, api: ActionAPI):
        self.api = api

    def actions(self):
        return self.api.catalog

    def execute(self, action, parameters, reason, dry_run, incident_id=""):
        code, d = self.api.execute({"action": action, "parameters": parameters, "reason": reason,
                                    "dry_run": dry_run, "incident_id": incident_id})
        return ActionResult(action=action, parameters=parameters, execution_id=d.get("execution_id"),
                            ok=code < 400 and d.get("ok", False), dry_run=dry_run, message=d.get("message", ""))

    def rollback(self, execution_id):
        code, d = self.api.rollback({"execution_id": execution_id})
        return ActionResult(action="rollback", execution_id=execution_id, ok=code < 400 and d.get("ok", False),
                            dry_run=False, message=d.get("message", ""))

    def kill(self):
        self.api.killed = True

    def resume(self):
        self.api.killed = False


def build(settings, store, bus, svcs, traces=None, fixes=None, llm: Optional[LLM] = None):
    rt = Runtime(svcs, fixes)
    client = InProcClient(ActionAPI(rt))
    ctx = Context(settings=settings, store=store, bus=bus, telemetry=fakes.telemetry(settings, svcs, traces),
                  actions=client, safety=Safety(store, bus, client, settings), llm=llm or LLM())  # type: ignore[arg-type]
    return Orchestrator(ctx), rt


def wait_phase(store, inc_id, phases, timeout=10.0) -> Incident:
    end = time.time() + timeout
    while time.time() < end:
        inc = store.load(inc_id)
        if inc and inc.phase in phases:
            return inc
        time.sleep(0.02)
    raise AssertionError(f"{inc_id} never reached {phases}; at {store.load(inc_id).phase}")


def tick_until_incident(orch, n=5) -> str:
    for _ in range(n):
        orch.sentinel.tick()
        incs = orch.ctx.store.list()
        if incs:
            return incs[-1].id
    raise AssertionError("no incident opened")


# ---------------------------------------------------------------- scenarios
def payment_failure():
    svcs = fakes.healthy()
    for s, r in (("payment", 0.6), ("checkout", 0.5), ("frontend", 0.3)):
        svcs[s].now = {"rps": 1.0, "error_ratio": r, "p95": 150}
    svcs["checkout"].outbound_errors = {"oteldemo.PaymentService/Charge": 0.6}
    svcs["payment"].logs = ["Payment request failed. Invalid token. app.loyalty.level=gold"]
    tr = fakes.payment_trace()
    return svcs, {"payment": [tr], "checkout": [tr], "frontend": [tr]}


def heal_all(svcs, service):
    for s in svcs.values():
        if s.now.get("rps", 0) > 0 or s.base.get("rps", 0) > 0:
            s.now = dict(s.base)
        s.state = "running"


def test_application_fault_dry_run_end_to_end(settings, store, bus):
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    inc = store.load(inc_id)
    assert {a.service for a in inc.alerts} >= {"payment", "checkout"}  # correlated into one incident
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    rc = inc.diagnosis.root_causes[0]
    assert (rc.component, rc.category) == ("payment", "application_error")
    assert inc.audits[-1].passed
    assert inc.plan.actions[0].action == "rollback_release" and inc.plan.actions[0].parameters == {"service": "payment"}
    assert inc.plan.actions[0].preview == "would rollback_release payment"
    assert all(dry for _, _, dry in rt.calls)  # nothing but previews before approval
    with pytest.raises(ApprovalError):
        orch.approval.decide(inc_id, "approved", "", inc.plan.change_hash)
    with pytest.raises(ApprovalError):
        orch.approval.decide(inc_id, "approved", "preet", "stale-hash")
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    inc = wait_phase(store, inc_id, ("dry_run_complete",))
    assert inc.execution.results[0].dry_run and inc.verification.recovered is None
    report, md = store.report(inc_id)
    validate(report)
    assert report["approval"] == {"required": True, "decision": "approved", "approver": "preet",
                                  "timestamp": inc.approval.timestamp}
    assert report["execution"]["action_api_only"] is True and report["proposed_action"]["dry_run"] is True
    assert report["root_cause"]["component"] == "payment"
    assert any("Invalid token" in e["detail"] or "Payment request failed" in e["detail"] for e in report["evidence"])
    assert "# INC-0001" in md
    assert (store.root / "submission.json").is_file()


def test_stopped_cache_resolved_live(settings, store, bus):
    svcs = fakes.healthy()
    svcs["valkey-cart"].state, svcs["valkey-cart"].exit_code = "exited", 137
    svcs["cart"].now = {"rps": 1.0, "error_ratio": 0.9, "p95": 40}
    svcs["cart"].logs = ["Wasn't able to connect to redis valkey-cart:6379: connection refused"]

    def start(s, service):
        heal_all(s, service)

    orch, rt = build(settings, store, bus, svcs, fixes={"start_service": start})
    orch.ctx.safety.set_dry_run(False, "preet")
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert inc.diagnosis.root_causes[0].component == "valkey-cart"
    assert inc.diagnosis.root_causes[0].category == "container_down"
    assert inc.plan.actions[0].action == "start_service"
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    inc = wait_phase(store, inc_id, ("resolved",))
    assert ("start_service", "valkey-cart", False) in rt.calls
    assert inc.verification.recovered is True and inc.resolved_at
    assert store.report(inc_id)[0]["status"] == "resolved"


def test_failed_recovery_rolls_back_and_escalates(settings, store, bus):
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces)  # the action fixes nothing
    orch.ctx.safety.set_dry_run(False, "preet")
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    inc = wait_phase(store, inc_id, ("escalated",))
    assert inc.verification.recovered is False and inc.execution.rollback_performed
    assert rt.undone and "recovery not confirmed" in inc.escalation_reason
    rep = store.report(inc_id)[0]
    assert rep["execution"]["rollback_performed"] is True and rep["verification"]["recovered"] is False


def test_reject_and_send_back(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    with pytest.raises(ApprovalError):
        orch.approval.decide(inc_id, "sent_back", "preet", inc.plan.change_hash, note="")
    orch.approval.decide(inc_id, "sent_back", "preet", inc.plan.change_hash, note="check valkey too")
    time.sleep(0.3)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert inc.rounds == 2 and inc.human_notes == ["preet: check valkey too"]
    orch.approval.decide(inc_id, "rejected", "preet", inc.plan.change_hash, note="not now")
    inc = wait_phase(store, inc_id, ("rejected",))
    assert not inc.execution.attempted
    assert store.report(inc_id)[0]["approval"]["decision"] == "rejected"


def test_approval_timeout_escalates(settings, store, bus):
    settings.approval_timeout_s = 0.3
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("escalated",))
    assert not inc.execution.attempted and "timeout" in inc.escalation_reason
    assert store.report(inc_id)[0]["approval"]["decision"] == "pending"


def test_kill_switch_freezes_execution(settings, store, bus):
    svcs, traces = payment_failure()
    orch, rt = build(settings, store, bus, svcs, traces, fixes={"rollback_release": heal_all})
    orch.ctx.safety.set_dry_run(False, "preet")
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    orch.ctx.safety.stop("preet", "drill")
    orch.approval.decide(inc_id, "approved", "preet", inc.plan.change_hash)
    time.sleep(0.4)
    assert store.load(inc_id).phase == "executing" and not any(not dry for _, _, dry in rt.calls)
    orch.ctx.safety.resume("preet")
    inc = wait_phase(store, inc_id, ("resolved",))
    assert ("rollback_release", "payment", False) in rt.calls


class ScriptedLLM(LLM):
    """A model that invents evidence: its diagnosis cites a quote that is in no evidence."""

    def __init__(self):
        super().__init__(client=object(), label="scripted")
        self.calls = 0

    def json(self, system, user, max_tokens=1500):
        self.calls += 1
        if "diagnose" in system:
            return {"summary": "payment is broken", "explained_alerts": ["A1", "A2", "A3"],
                    "root_causes": [{"component": "payment", "category": "application_error", "cause": "bad deploy",
                                     "confidence": 0.99, "claims": [{"text": "x", "evidence_id": "E2",
                                                                     "quote": "error_ratio=0.999 everywhere"}]}]}
        return None


def test_invented_evidence_is_rejected_then_rules_take_over(settings, store, bus):
    svcs, traces = payment_failure()
    llm = ScriptedLLM()
    orch, _ = build(settings, store, bus, svcs, traces, llm=llm)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    assert [a.passed for a in inc.audits][:2] == [False, False] and inc.audits[-1].passed
    assert inc.diagnosis.produced_by == "rules" and inc.rounds == 3
    assert any("is not in E2" in p for p in inc.audits[0].problems)


def test_no_flag_data_anywhere(settings, store, bus):
    svcs, traces = payment_failure()
    traces["payment"][0]["spans"][3]["tags"].append({"key": "feature_flag.key", "value": "paymentFailure"})
    orch, _ = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    wait_phase(store, inc_id, ("awaiting_approval",))
    for ev in store.evidence(inc_id):
        assert not mentions_flags(ev.content + str(ev.data) + str(ev.args))


def test_sentinel_debounce_and_suppression(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    assert orch.sentinel.tick() == []  # first sighting is not yet an alert
    assert orch.sentinel.tick()        # confirmed on the second
    inc = store.list()[0]
    inc.phase = "rejected"
    store.save(inc)
    orch.sentinel.tick()
    assert len(store.list()) == 1      # same signals after an unresolved close: suppressed


class ScriptedToolModel:
    """Stands in for a LangChain chat model in the Investigator's LangGraph loop."""

    def __init__(self, script):
        self.script, self.seen = list(script), []

    def bind_tools(self, tools):
        self.tool_names = [t.name for t in tools]
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        self.seen.append(messages[-1])
        name, args = self.script.pop(0)
        return AIMessage(content=f"calling {name}", tool_calls=[{"name": name, "args": args, "id": f"c{len(self.seen)}"}])


def test_investigator_llm_loop_runs_tools_and_finishes(settings, store, bus):
    from incident_zero.agents.investigator import Investigator
    from incident_zero.evidence import EvidenceLedger

    svcs, traces = payment_failure()
    model = ScriptedToolModel([("overview", {}), ("service_metrics", {"service": "payment"}),
                               ("search_logs", {"service": "flagd"}),
                               ("finish_investigation", {"summary": "payment fails charges, see E1 E2"})])
    orch, _ = build(settings, store, bus, svcs, traces, llm=LLM(tool_model=model, label="scripted"))
    orch.sentinel.on_new_incident = lambda _id: None  # drive the Investigator directly, no orchestrator worker
    inc_id = tick_until_incident(orch)
    inc = store.load(inc_id)
    ledger = EvidenceLedger(store, bus, inc_id)
    summary = Investigator(orch.ctx).run(inc, ledger)
    assert summary == "payment fails charges, see E1 E2"
    assert "finish_investigation" in model.tool_names and "promql" in model.tool_names
    tools = [e.tool for e in ledger.all()]
    assert tools.count("overview") == 1 and "service_metrics" in tools
    assert "refused" in model.seen[3].content  # the flagd log request was refused, not recorded
