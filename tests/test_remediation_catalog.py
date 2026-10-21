"""The remediation catalog against a realistic compose file (tests/fixtures/shop) and a static topology. No Docker."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from faultline.remediation import catalog as C
from faultline.remediation import override as ov
from faultline.schemas import Finding
from faultline.topology import datastores
from faultline.topology.compose import load_compose
from faultline.topology.graph import build

ROOT = Path(__file__).resolve().parent.parent
SHOP = ROOT / "tests" / "fixtures" / "shop"
COMPOSE = SHOP / "docker-compose.yml"
SHOP_APPS = ["auth", "orders", "orders-search", "notifier", "gateway"]
LABEL = "deploy/docker-compose.override.yml"


@pytest.fixture(scope="module")
def topo():
    return build(str(COMPOSE), "shop", str(SHOP / "nginx" / "nginx.conf"),
                 str(SHOP / "shop_knowledge.yaml"), live=False)


@pytest.fixture
def base():
    return load_compose(COMPOSE)


def _finding(code: str, title: str) -> Finding:
    return Finding(code=code, severity="high", title=title, evidence="synthetic")


# ---------------------------------------------------------------- catalog shape
def test_catalog_ids_order_and_flags():
    assert list(C.CATALOG) == ["add_or_tighten_healthcheck", "add_dependency_healthcheck", "add_healthy_dependency",
                               "set_restart_policy", "restart_service_now", "set_resource_limits"]
    for fid, fix in C.CATALOG.items():
        assert fix.id == fid and fix.is_reversible and not fix.test_only and not fix.protected_ok
    assert [f.id for f in C.CATALOG.values() if f.runtime_only] == ["restart_service_now"]


def test_for_finding_priority_order():
    assert [f.id for f in C.for_finding("manual_restart_required")] == ["set_restart_policy", "restart_service_now"]
    assert [f.id for f in C.for_finding("health_check_masked_failure")] == ["add_or_tighten_healthcheck"]
    assert [f.id for f in C.for_finding("dependency_blind_readiness")] == [
        "add_or_tighten_healthcheck", "add_dependency_healthcheck", "add_healthy_dependency"]
    assert [f.id for f in C.for_finding("slow_recovery")] == ["set_resource_limits"]
    assert C.for_finding("api_impact") == []


def test_test_only_entry_hidden_by_default(monkeypatch):
    monkeypatch.delenv(C.TEST_CATALOG_ENV, raising=False)
    assert "break_healthcheck_for_test" not in C.CATALOG
    assert C.get("break_healthcheck_for_test") is None
    assert all(not f.test_only for f in C.catalog_entries())
    assert "break_healthcheck_for_test" not in [f.id for f in C.for_finding("health_check_masked_failure")]
    fix = C.get("break_healthcheck_for_test", include_test=True)
    assert fix is not None and fix.test_only
    assert [f.id for f in C.for_finding("health_check_masked_failure", include_test=True)][-1] == fix.id
    monkeypatch.setenv(C.TEST_CATALOG_ENV, "1")
    assert C.get("break_healthcheck_for_test") is fix


def test_the_targets_thresholds_protect_its_datastore():
    assert C.protected_services() == set()          # core's own thresholds name no application
    prot = C.protected_services(thresholds_file=str(SHOP / "thresholds.yaml"))
    assert prot == {"postgres"}
    assert C.CATALOG["restart_service_now"].requires_allow_protected(["postgres"], prot)
    assert C.CATALOG["add_dependency_healthcheck"].requires_allow_protected(["postgres"], prot)
    assert not C.CATALOG["restart_service_now"].requires_allow_protected(["orders"], prot)


# ---------------------------------------------------------------- targets
def test_masked_failure_targets_the_named_service_only(topo, make_report):
    fix = C.CATALOG["add_or_tighten_healthcheck"]
    f = _finding("health_check_masked_failure", "orders-search kept reporting /ready=200 while the API was failing")
    report = make_report(service="orders", failure_type="container_crash", affected=["orders"], findings=[f])
    assert fix.targets(report, topo, f) == ["orders-search"]     # not `orders`, hyphen-aware


def test_dependency_blind_targets_the_dependents_not_the_target(topo, make_report, base):
    fix = C.CATALOG["add_or_tighten_healthcheck"]
    f = _finding("dependency_blind_readiness",
                 "Dependents stayed 'ready' while auth was down: orders (100%), gateway (98%)")
    report = make_report(service="auth", findings=[f])
    assert fix.targets(report, topo, f, base) == ["gateway", "orders"]


def test_targets_filter_roles_and_base(topo, make_report, base):
    report = make_report(service="nginx", affected=["gateway", "postgres", "web-ui", "nope"])
    assert C.CATALOG["add_or_tighten_healthcheck"].targets(report, topo, None, base) == ["gateway"]
    assert C.CATALOG["restart_service_now"].targets(report, topo, None, base) == ["nginx", "gateway", "postgres"]
    trimmed = copy.deepcopy(base)
    del trimmed["services"]["gateway"]
    assert C.CATALOG["add_or_tighten_healthcheck"].targets(report, topo, None, trimmed) == []


def test_healthy_dependency_targets_env_dependents_of_the_datastore(topo, make_report):
    report = make_report(service="postgres", affected=["gateway"])
    f = _finding("cascade_beyond_hypothesis", "Blast radius exceeded the hypothesis: ['gateway']")
    # gateway reaches no datastore through its env, so it is not a target; postgres's env dependents are
    assert C.CATALOG["add_healthy_dependency"].targets(report, topo, f) == [
        "auth", "notifier", "orders", "orders-search"]
    assert C.CATALOG["add_dependency_healthcheck"].targets(report, topo, f) == ["postgres"]


# ---------------------------------------------------------------- add_or_tighten_healthcheck
def test_healthcheck_swaps_alive_for_ready_keeping_anchor_timings(topo, base):
    fix = C.CATALOG["add_or_tighten_healthcheck"]
    change = fix.build(base, {}, SHOP_APPS, topo)
    assert change is not None and change.services == SHOP_APPS and not change.runtime_only
    anchor = base["services"]["orders"]["healthcheck"]          # the resolved x-shop-healthcheck anchor
    for s in SHOP_APPS:
        hc = change.override_after["services"][s]["healthcheck"]
        assert hc["test"] == "curl --fail `hostname`:7626/ready || exit 1"
        assert {k: hc[k] for k in ("interval", "timeout", "retries", "start_period")} == {
            "interval": anchor["interval"], "timeout": anchor["timeout"], "retries": anchor["retries"],
            "start_period": anchor["start_period"]}
    assert ov.applied_fixes(change.override_after) == ["add_or_tighten_healthcheck"]
    assert "/ready instead of /alive" in change.summary and "the re-run decides" in change.risks


def test_healthcheck_preview_is_the_real_override_diff(topo, base):
    fix = C.CATALOG["add_or_tighten_healthcheck"]
    change = fix.build(base, {}, ["orders"], topo)
    text = fix.preview(change, "", LABEL)
    assert text == ov.diff("", ov.render(change.override_after), LABEL)
    assert text.startswith(f"--- /dev/null\n+++ b/{LABEL}\n")
    assert "+      test: curl --fail `hostname`:7626/ready || exit 1\n" in text
    assert text == fix.preview(fix.build(base, {}, ["orders"], topo), "", LABEL)   # deterministic -> stable hash


def test_healthcheck_none_when_already_applied_or_already_ready(topo, base):
    fix = C.CATALOG["add_or_tighten_healthcheck"]
    first = fix.build(base, {}, ["orders"], topo)
    again, why = fix.build_with_reason(base, first.override_after, ["orders"], topo)
    assert again is None and "already" in why
    ready = copy.deepcopy(base)
    ready["services"]["orders"]["healthcheck"] = {"test": "curl --fail localhost:7626/ready || exit 1"}
    assert fix.build(ready, {}, ["orders"], topo) is None
    assert fix.build(base, {}, ["postgres"], topo) is None       # an engine ping: no /alive to tighten


def test_healthcheck_list_form_and_incremental_diff(topo, base):
    fix = C.CATALOG["add_or_tighten_healthcheck"]
    listed = copy.deepcopy(base)
    listed["services"]["gateway"]["healthcheck"]["test"] = ["CMD-SHELL", "curl --fail `hostname`:7626/alive || exit 1"]
    first = fix.build(listed, {}, ["orders"], topo)
    second = fix.build(listed, first.override_after, ["gateway"], topo)
    assert second.override_after["services"]["gateway"]["healthcheck"]["test"] == [
        "CMD-SHELL", "curl --fail `hostname`:7626/ready || exit 1"]
    assert second.override_after["services"]["orders"] == first.override_after["services"]["orders"]
    d = fix.preview(second, ov.render(first.override_after), LABEL)
    assert "+  gateway:\n" in d and "-  orders:" not in d


def test_build_does_not_mutate_inputs(topo, base):
    before_base, current = copy.deepcopy(base), {"services": {"orders": {"restart": "always"}}}
    snapshot = copy.deepcopy(current)
    C.CATALOG["add_or_tighten_healthcheck"].build(base, current, SHOP_APPS, topo)
    assert base == before_base and current == snapshot


# ---------------------------------------------------------------- set_restart_policy
def test_restart_policy_is_none_when_every_service_has_one(topo, base):
    change, why = C.CATALOG["set_restart_policy"].build_with_reason(base, {}, SHOP_APPS + ["postgres", "nginx"], topo)
    assert change is None and "restart: always" in why


def test_restart_policy_on_compose_lacking_one(topo):
    synthetic = {"services": {"orders": {"image": "x"}, "gateway": {"image": "y", "restart": "no"},
                              "auth": {"image": "z", "restart": "on-failure"}}}
    fix = C.CATALOG["set_restart_policy"]
    change = fix.build(synthetic, {}, ["orders", "gateway", "auth"], topo)
    assert change.services == ["orders", "gateway"]
    assert change.override_after["services"]["orders"] == {"restart": "unless-stopped"}
    assert "auth" not in change.override_after["services"]
    assert fix.build(synthetic, change.override_after, ["orders", "gateway"], topo) is None


# ---------------------------------------------------------------- dependency entries
def test_healthy_dependency_merges_list_form_depends_on(topo, base):
    fix = C.CATALOG["add_healthy_dependency"]
    assert base["services"]["orders"]["depends_on"] == ["auth"]
    change = fix.build(base, {}, ["orders", "auth"], topo)
    assert change.services == ["orders", "auth"]
    assert change.override_after["services"]["orders"]["depends_on"] == {
        "auth": {"condition": "service_started"}, "postgres": {"condition": "service_healthy"}}
    assert change.override_after["services"]["auth"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert "only take effect" in change.risks and "runtime" in change.risks
    assert fix.build(base, change.override_after, ["orders"], topo) is None


def test_healthy_dependency_keeps_mapping_entries(topo, base):
    mapped = copy.deepcopy(base)
    mapped["services"]["orders"]["depends_on"] = {"auth": {"condition": "service_started", "required": True}}
    change = C.CATALOG["add_healthy_dependency"].build(mapped, {}, ["orders"], topo)
    assert change.override_after["services"]["orders"]["depends_on"]["auth"] == {
        "condition": "service_started", "required": True}


def test_healthy_dependency_skips_services_without_env_edge(topo, base):
    change, why = C.CATALOG["add_healthy_dependency"].build_with_reason(base, {}, ["gateway"], topo)
    assert change is None and "no datastore" in why


def test_healthy_dependency_needs_a_datastore_healthcheck(topo, base):
    """A datastore without a healthcheck: compose would refuse service_healthy, so no change."""
    live_like = copy.deepcopy(base)
    del live_like["services"]["postgres"]["healthcheck"]
    fix = C.CATALOG["add_healthy_dependency"]
    change, why = fix.build_with_reason(live_like, {}, ["orders"], topo)
    assert change is None and "add_dependency_healthcheck" in why

    dep_hc = C.CATALOG["add_dependency_healthcheck"]
    first = dep_hc.build(live_like, {}, ["postgres"], topo)
    engine_hc = datastores.ENGINES["postgres"].healthcheck
    assert first.override_after["services"]["postgres"]["healthcheck"] == engine_hc
    # the engine's check is exactly what the fixture's own compose file declares
    assert engine_hc == {k: base["services"]["postgres"]["healthcheck"][k]
                         for k in ("test", "interval", "timeout", "retries", "start_period")}
    assert dep_hc.requires_allow_protected(first.services, C.protected_services(thresholds_file=str(SHOP / "thresholds.yaml")))
    second = fix.build(live_like, first.override_after, ["orders"], topo)
    assert second is not None
    assert ov.applied_fixes(second.override_after) == ["add_dependency_healthcheck", "add_healthy_dependency"]
    assert dep_hc.build(base, {}, ["postgres"], topo) is None      # the fixture compose already has one


# ---------------------------------------------------------------- set_resource_limits
def test_resource_limits_cpu_from_jvm_processor_count(topo, base):
    change = C.CATALOG["set_resource_limits"].build(base, {}, ["orders", "postgres"], topo)
    lim = change.override_after["services"]
    assert lim["orders"]["deploy"]["resources"]["limits"] == {"cpus": 2.0}       # -XX:ActiveProcessorCount=2
    assert lim["postgres"]["deploy"]["resources"]["limits"] == {"cpus": 1.0}      # no JVM hint: default
    assert "clamped" not in change.summary and "memory" in change.summary


def test_resource_limits_clamp_is_recorded(topo):
    synthetic = {"services": {"orders": {"environment": ["JAVA_TOOL_OPTIONS=-Xss512k -XX:ActiveProcessorCount=8"],
                                         "deploy": {"resources": {"limits": {"memory": "512m"}}}}}}
    change = C.CATALOG["set_resource_limits"].build(synthetic, {}, ["orders"], topo)
    assert change.override_after["services"]["orders"]["deploy"]["resources"]["limits"] == {"cpus": C.CPU_MAX}
    assert "clamped" in change.summary and "8 -> 2" in change.summary


def test_resource_limits_existing_cpu_left_alone_and_memory_never_lowered(topo, base):
    fix = C.CATALOG["set_resource_limits"]
    with_cpu = copy.deepcopy(base)
    with_cpu["services"]["orders"]["deploy"]["resources"]["limits"]["cpus"] = "0.75"
    assert fix.build(with_cpu, {}, ["orders"], topo) is None
    lowered = {"services": {"orders": {"deploy": {"resources": {"limits": {"memory": "128m"}}}}}}
    change = fix.build(base, lowered, ["orders"], topo)
    assert change.override_after["services"]["orders"]["deploy"]["resources"]["limits"] == {
        "cpus": 2.0, "memory": "512m"}


# ---------------------------------------------------------------- runtime-only and test-only
def test_restart_now_is_runtime_only_and_previews_the_command(topo, base):
    fix = C.CATALOG["restart_service_now"]
    current = {"services": {"orders": {"restart": "always"}}}
    change = fix.build(base, current, ["orders"], topo)
    assert change.runtime_only and change.action == "docker restart shop-orders-1"
    assert fix.preview(change, ov.render(current), LABEL) == "docker restart shop-orders-1"
    assert change.override_after == current
    assert change.rollback == "nothing to roll back; the container restarts with its existing configuration"


def test_break_healthcheck_for_test_always_fails(topo, base):
    fix = C.get("break_healthcheck_for_test", include_test=True)
    change = fix.build(base, {}, ["orders"], topo)
    hc = change.override_after["services"]["orders"]["healthcheck"]
    assert hc["test"] == ["CMD-SHELL", "exit 1"] and hc["retries"] == 1
    assert change.summary.startswith("TEST ONLY")
    assert fix.build(base, change.override_after, ["orders"], topo) is None


def test_every_config_change_renders_valid_yaml(topo, base):
    for fix in C.catalog_entries(include_test=True):
        svcs = ["postgres"] if fix.roles == ("datastore",) else ["orders"]
        no_hc = copy.deepcopy(base)
        del no_hc["services"]["postgres"]["healthcheck"]
        change = fix.build(no_hc if fix.id == "add_dependency_healthcheck" else base, {}, svcs, topo)
        if change is None:
            assert fix.id == "set_restart_policy"
            continue
        if not change.runtime_only:
            rendered = ov.render(change.override_after)
            assert ov.load_yaml_text(rendered) == change.override_after
            assert change.summary and change.risks and change.rollback and change.expected
