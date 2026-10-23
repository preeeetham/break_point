from __future__ import annotations

from incident_zero.actions.catalog import MOCK_ACTIONS
from incident_zero.agents.planner import rule_actions
from incident_zero.models import Diagnosis, Evidence, RootCause

RB = {"service_flags": {"payment": ["paymentFailure"], "load-generator": ["loadGeneratorFloodHomepage"]},
      "starved_cpu_limit": 0.25, "starved_memory_limit_mb": 64}


def _ev(content: str, eid: str = "E1", source: str = "docker", tool: str = "container_status") -> Evidence:
    return Evidence(id=eid, source=source, tool=tool, summary="s", content=content)  # type: ignore[arg-type]


def _diag(component: str, category: str) -> Diagnosis:
    return Diagnosis(summary="s", root_causes=[RootCause(component=component, category=category,  # type: ignore[arg-type]
                                                         cause="c", confidence=0.8)])


def _container(svc, cpu="None", mem="None", networks="opentelemetry-demo", state="running", oom="false"):
    return (f"{svc}: state={state} exit_code=0 oom_killed={oom} restarts=0 uptime_s=10 health=None "
            f"networks={networks} cpu_limit={cpu} memory_limit_mb={mem}")


def test_cpu_starvation_by_limit_escalates():
    ev = [_ev(_container("checkout", cpu="0.02")),
          _ev("checkout: cpu_percent=2.0 throttled_periods_delta=40 memory_mb=50 memory_limit_mb=None "
              "memory_percent=None net_rx_errors=0 net_tx_dropped=0 pids=9", "E2", tool="container_resources")]
    acts, notes = rule_actions(_diag("checkout", "resource_cpu"), ev, MOCK_ACTIONS, RB)
    assert acts == [] and "no allowed action raises limits" in notes[0]


def test_detached_network_escalates_and_netem_restarts():
    acts, notes = rule_actions(_diag("currency", "network"), [_ev(_container("currency", networks="none"))],
                               MOCK_ACTIONS, RB)
    assert acts == [] and "detached" in notes[0]
    acts, _ = rule_actions(_diag("quote", "network"), [_ev(_container("quote"))], MOCK_ACTIONS, RB)
    assert [(a.action, a.parameters) for a in acts] == [("restart_service", {"service": "quote"})]


def test_app_fault_toggles_flags_and_flood_throttles():
    acts, _ = rule_actions(_diag("payment", "application_error"), [], MOCK_ACTIONS, RB)
    assert [(a.action, a.parameters) for a in acts] == [("toggle_feature_flag", {"flag": "paymentFailure",
                                                                                 "enabled": False})]
    acts, _ = rule_actions(_diag("frontend-proxy", "traffic"), [], MOCK_ACTIONS, RB, normal_users=5)
    assert [a.action for a in acts] == ["throttle_load_generator", "toggle_feature_flag"]
    assert acts[0].parameters == {"users": 5}


def test_unknown_service_app_fault_falls_back_to_restart():
    acts, _ = rule_actions(_diag("quote", "application_error"), [], MOCK_ACTIONS, RB)
    assert [(a.action, a.parameters) for a in acts] == [("restart_service", {"service": "quote"})]
