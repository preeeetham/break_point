"""Fix proposals: code-selected candidates, templated explanations, strictly validated LLM step, CLI (Docker-free)."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from faultline import cli
from faultline.agents import fix as fix_agent
from faultline.config import Settings
from faultline.remediation import catalog, override, propose
from faultline.remediation import proposals as P
from faultline.reporting.store import ReportStore
from faultline.schemas import Finding, HealthSummary
from faultline.topology.graph import build

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
NGINX = ROOT / "deploy" / "nginx" / "nginx.conf"
KNOWLEDGE = ROOT / "config" / "ditto_knowledge.yaml"
PROJECT = "faultline-test"
EID = "exp-things-crash"


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    """override.base_config asks the live project first; tests must never reach Docker, so it falls back to
    the yaml of settings.compose_file (the real deploy/docker-compose.yml)."""
    monkeypatch.setattr(override, "live_project", lambda *a, **k: None)
    monkeypatch.delenv(catalog.TEST_CATALOG_ENV, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        compose_project=PROJECT, compose_file=str(COMPOSE), nginx_conf=str(NGINX), knowledge_file=str(KNOWLEDGE),
        reports_dir=str(tmp_path / "reports"), thresholds_file=str(ROOT / "config" / "thresholds.yaml"),
        remediation_override_file=str(tmp_path / "deploy" / "docker-compose.override.yml"),
        llm_enabled=False, fix_proposal_timeout_s=1800.0,
    )


@pytest.fixture
def store(settings) -> ReportStore:
    st = ReportStore(settings.reports_dir)
    st.write_topology(build(str(COMPOSE), PROJECT, str(NGINX), str(KNOWLEDGE), live=False))
    return st


@pytest.fixture
def report(make_report, store):
    """A things container_crash where things' /ready stayed green and its dependents never noticed."""
    r = make_report(
        experiment_id=EID, service="things", failure_type="container_crash", during_success=0.12, ttr=41.0,
        affected=["gateway", "things-search", "connectivity"],
        health=[HealthSummary(service="things", ticks=30, alive_ok_fraction=1.0, ready_ok_fraction=1.0)],
        findings=[
            Finding(code="api_impact", severity="high", title="API success dropped to 12% during the fault",
                    evidence="120 requests, p95 800 ms (baseline 60 ms)"),
            Finding(code="health_check_masked_failure", severity="high",
                    title="things kept reporting /ready=200 while the API was failing",
                    evidence="/ready OK 100% of 30 checks while API success was 12%; cf. Ditto issue #1865"),
            Finding(code="dependency_blind_readiness", severity="low",
                    title="Dependents stayed 'ready' while things was down: gateway (100%), things-search (100%)",
                    evidence="readiness only reflects the service's own process"),
            Finding(code="auto_recovered", severity="info", title="Recovered on its own in 41s", evidence="-"),
        ],
    )
    store.write_experiment(r)
    store.write_fix(fix_agent.recommend(r, None, None))
    return r


class StubLLM:
    """Answers chat_json with a canned dict and records how often it was asked."""

    def __init__(self, answer, available=True):
        self.answer, self._available, self.calls, self.enabled = answer, available, 0, True

    def available(self):
        return self._available

    def describe(self):
        return "stub:llm"

    def chat_json(self, system, user, max_tokens=700):
        self.calls += 1
        return self.answer(user) if callable(self.answer) else self.answer


def _expected_preview(settings, store, p):
    """Rebuild the change independently, the way the apply engine will, and render its preview."""
    fx = catalog.get(p.catalog_fix_id)
    base = override.base_config(settings)
    change = fx.build(base, override.load(settings.remediation_override_file), p.target_services, store.topology())
    return catalog.preview(change, override.read_text(settings.remediation_override_file), propose._path_label(settings))


# ---------------------------------------------------------------- candidates and proposals
def test_candidates_are_code_selected_from_the_catalog(settings, store, report):
    cands = propose.candidates(settings, store, EID)
    pairs = {(c.fix.id, tuple(c.services)) for c in cands}
    # /alive -> /ready on things (named by the masked-failure finding) and on its blind dependents
    assert ("add_or_tighten_healthcheck", ("things",)) in pairs
    assert ("add_or_tighten_healthcheck", ("gateway", "things-search")) in pairs
    assert all(catalog.get(c.fix.id) is not None for c in cands)
    assert all(c.finding.severity != "info" for c in cands)
    assert len(pairs) == len(cands)                    # deduplicated by (fix, services)
    # the base compose already gives mongodb a healthcheck, so that catalog entry has nothing to change
    assert not any(c.fix.id == "add_dependency_healthcheck" for c in cands)
    hc = next(c for c in cands if c.fix.id == "add_or_tighten_healthcheck" and c.services == ["things"])
    assert hc.motivated_by                               # the Fix agent's recommendation for this finding
    assert not hc.requires_allow_protected


def test_proposals_have_code_built_preview_and_hash(settings, store, report):
    made = propose.build_proposals(settings, store, EID, llm=None, log=lambda m: None)
    assert made
    for rank, p in enumerate(made, 1):
        assert p.status == "pending" and p.rank == rank and p.ranked_by == "code"
        assert p.change_hash == P.change_hash(p.change_preview)
        assert p.change_preview == _expected_preview(settings, store, p)
        assert p.audit[0].actor == "faultline" and "proposed by faultline" in p.audit[0].note
        assert timedelta(seconds=1700) < p.expires_at - p.created_at <= timedelta(seconds=1801)
        assert store.proposal(p.proposal_id) == p
    things = next(p for p in made if p.target_services == ["things"])
    assert "-    test: curl --fail `hostname`:7626/alive" not in things.change_preview  # the override only adds
    assert "+      test: curl --fail `hostname`:7626/ready || exit 1" in things.change_preview
    assert not Path(settings.remediation_override_file).exists()                      # nothing was applied


def test_template_explanation_without_llm(settings, store, report):
    made = propose.build_proposals(settings, store, EID, llm=None, log=lambda m: None)
    p = next(x for x in made if x.target_services == ["things"])
    e = p.explanation
    assert e.generated_by == "template"
    for part in (e.problem, e.change, e.expected_improvement, e.risks, e.rollback):
        assert part.strip()
        assert "—" not in part
    assert "ready while real requests were failing" in e.problem and "12%" in e.problem and "41" in e.problem
    assert "Exact change (shown verbatim below)" in e.change
    assert "byte for byte" in e.rollback
    metrics = {m.metric: m for m in p.expected_metrics}
    assert metrics["time_to_recover_s"].before == 41.0 and metrics["time_to_recover_s"].expected_after.startswith("<= 41")
    assert metrics["error_rate_during"].before == pytest.approx(0.88)
    assert metrics["p95_during_ms"].before == 800
    assert metrics["finding:health_check_masked_failure"].before == 1.0
    md = (store.root / "proposals" / f"{p.proposal_id}.md").read_text()
    assert "```diff\n" + p.change_preview.rstrip("\n") + "\n```" in md
    for heading in ("What is wrong", "Exactly what will change", "Expected improvement", "Risks", "Rollback"):
        assert heading in md
    text = propose.render_text(p)
    assert p.change_preview.rstrip("\n") in text and p.change_hash in text
    assert f"faultline approve {p.proposal_id}" in text and f"faultline reject {p.proposal_id}" in text


def test_disabled_llm_is_never_called(settings, store, report):
    llm = StubLLM({"ranking": []}, available=False)
    made = propose.build_proposals(settings, store, EID, llm=llm, log=lambda m: None)
    assert made and llm.calls == 0
    assert all(p.explanation.generated_by == "template" and p.ranked_by == "code" for p in made)


def test_protected_service_needs_allow_protected(settings, store, report):
    # restart_service_now addresses manual_restart_required; aimed at mongodb (protected in thresholds.yaml)
    r = report.model_copy(update={"experiment_id": "exp-mongo", "target": {"service": "mongodb"},
                                  "affected_services_observed": [],
                                  "findings": [Finding(code="manual_restart_required", severity="high",
                                                       title="Recovery needed restarts beyond the rollback",
                                                       evidence="restart mongodb")]})
    store.write_experiment(r)
    made = propose.build_proposals(settings, store, "exp-mongo", log=lambda m: None)
    p = next(x for x in made if x.catalog_fix_id == "restart_service_now")
    assert p.requires_allow_protected
    assert "--allow-protected" in p.explanation.risks
    assert p.change_preview.startswith("docker restart ")


def test_mongodb_healthcheck_proposal_is_flagged_protected(settings, store, report):
    # The live upstream stack runs mongodb without a healthcheck: build against such a base config.
    base = override.base_config(settings)
    del base["services"]["mongodb"]["healthcheck"]
    made = propose.build_proposals(settings, store, EID, base=base, log=lambda m: None)
    p = next(x for x in made if x.catalog_fix_id == "add_dependency_healthcheck")
    assert p.target_services == ["mongodb"]
    assert p.requires_allow_protected is True
    assert "--allow-protected" in p.explanation.risks and "--allow-protected" in propose.render_text(p)
    assert all(not x.requires_allow_protected for x in made if "mongodb" not in x.target_services)


# ---------------------------------------------------------------- the LLM step is validated strictly
def test_llm_naming_an_off_catalog_fix_is_discarded(settings, store, report):
    logs: list[str] = []
    llm = StubLLM({"ranking": ["drop_database:mongodb"],
                   "rewrites": {"drop_database:mongodb": {"problem": "Mongo is slow, drop it."}}})
    made = propose.build_proposals(settings, store, EID, llm=llm, log=logs.append)
    assert llm.calls == 1
    assert {p.catalog_fix_id for p in made} <= set(catalog.CATALOG)
    assert all(p.ranked_by == "code" and p.explanation.generated_by == "template" for p in made)
    assert any("not on the code-built shortlist" in m for m in logs)
    assert "drop_database" in made[0].audit[0].note


def test_llm_ranking_is_applied_and_diff_text_rejected(settings, store, report):
    ids = [c.id for c in propose.candidates(settings, store, EID)]
    assert len(ids) >= 2
    first, last = ids[0], ids[-1]

    def answer(user):
        return {"ranking": [last, last, "made_up:x"],
                "rewrites": {
                    last: {"problem": "services:\n  things:\n    healthcheck: {}",           # YAML: rejected
                           "risks": "Recreating things takes about two minutes; the cluster rejoins on its own.",
                           "change": "+ restart: always",                                   # never the model's
                           "rollback": "just delete the file"},
                    first: {"expected_improvement": "x" * 700,                             # too long: rejected
                            "problem": "--- a/x\n+++ b/x\n@@ -1 +1 @@"}}}                  # a diff: rejected

    made = propose.build_proposals(settings, store, EID, llm=StubLLM(answer), log=lambda m: None)
    by_id = {f"{p.catalog_fix_id}:{'+'.join(p.target_services)}": p for p in made}
    top = by_id[last]
    assert top.rank == 1 and all(p.ranked_by == "llm" for p in made)
    assert sorted(p.rank for p in made) == list(range(1, len(made) + 1))       # a permutation, nothing lost
    assert top.explanation.generated_by == "llm"
    assert top.explanation.risks.startswith("Recreating things takes about two minutes")
    assert "healthcheck: {}" not in top.explanation.problem                      # template kept
    assert "Exact change (shown verbatim below)" in top.explanation.change       # code-built
    assert "byte for byte" in top.explanation.rollback
    assert top.change_hash == P.change_hash(top.change_preview)
    other = by_id[first]
    assert other.explanation.generated_by == "template"
    assert "x" * 700 not in other.explanation.expected_improvement


def test_apply_llm_answer_unit():
    out = propose.apply_llm_answer({"ranking": ["b", "zzz", "b", 3], "rewrites": {"a": {"risks": "- bullet"}}},
                                   ["a", "b", "c"])
    assert out.order == ["b", "a", "c"] and out.ranked_by_llm
    assert out.rewrites == {}
    assert len(out.notes) == 3
    assert propose.apply_llm_answer("nope", ["a"]).order == ["a"]
    assert propose.valid_llm_text("  plain words  ") == "plain words"
    assert propose.valid_llm_text("@@ -1 +1 @@") is None and propose.valid_llm_text("") is None


# ---------------------------------------------------------------- dedup
def test_duplicate_proposals_are_not_recreated(settings, store, report):
    first = propose.build_proposals(settings, store, EID, log=lambda m: None)
    again = propose.build_proposals(settings, store, EID, log=lambda m: None)
    assert first and again == []
    assert len(store.proposals()) == len(first)
    # a rejected proposal no longer blocks: asking again re-proposes exactly that one
    P.reject(store, first[0].proposal_id, "test")
    third = propose.build_proposals(settings, store, EID, log=lambda m: None)
    assert [(p.catalog_fix_id, p.target_services) for p in third] == [(first[0].catalog_fix_id,
                                                                       first[0].target_services)]


def test_unknown_experiment_raises(settings, store):
    with pytest.raises(KeyError):
        propose.build_proposals(settings, store, "nope", log=lambda m: None)


# ---------------------------------------------------------------- CLI
@pytest.fixture
def cli_env(monkeypatch, settings, store, report):
    monkeypatch.setenv("FAULTLINE_REPORTS_DIR", settings.reports_dir)
    monkeypatch.setenv("FAULTLINE_OVERRIDE_FILE", settings.remediation_override_file)
    monkeypatch.setenv("FAULTLINE_COMPOSE_FILE", settings.compose_file)
    monkeypatch.setenv("FAULTLINE_COMPOSE_PROJECT", PROJECT)
    monkeypatch.setenv("FAULTLINE_KNOWLEDGE_FILE", settings.knowledge_file)
    monkeypatch.setenv("FAULTLINE_LLM", "false")
    return store


def _pending(store):
    return [p for p in store.proposals() if p.status == "pending"]


def test_cli_propose_list_show_and_reject(cli_env, capsys):
    store = cli_env
    assert cli.main(["propose", EID, "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "Can I fix this?" in out and "exact change (verbatim" in out
    made = _pending(store)
    assert made

    assert cli.main(["proposals"]) == 0
    listing = capsys.readouterr().out
    assert all(p.proposal_id in listing for p in made)

    pid = made[0].proposal_id
    assert cli.main(["proposals", "--id", pid]) == 0
    shown = capsys.readouterr().out
    assert made[0].change_hash in shown and "audit:" in shown

    assert cli.main(["reject", pid, "--reason", "not now"]) == 0
    p = store.proposal(pid)
    assert p.status == "rejected" and p.reason == "not now" and p.decided_by.startswith("cli:")
    assert cli.main(["reject", pid]) == 1                              # final: cannot be rejected twice
    assert cli.main(["propose", "missing-exp", "--no-llm"]) == 1


def test_cli_approve_with_wrong_hash_is_refused(cli_env, capsys):
    store = cli_env
    cli.main(["propose", EID, "--no-llm"])
    p = _pending(store)[0]
    assert cli.main(["approve", p.proposal_id, "--hash", "0123456789abcdef", "--no-apply"]) == 1
    assert "refused" in capsys.readouterr().out
    assert store.proposal(p.proposal_id).status == "pending"
    assert cli.main(["approve", p.proposal_id, "--hash", "0" * 64, "--no-apply"]) == 1
    assert store.proposal(p.proposal_id).status == "pending"


def test_cli_approve_interactive_confirmation(cli_env, monkeypatch, capsys):
    store = cli_env
    cli.main(["propose", EID, "--no-llm"])
    a, b = _pending(store)[:2]
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope1234")
    assert cli.main(["approve", a.proposal_id, "--no-apply"]) == 1
    assert store.proposal(a.proposal_id).status == "pending"
    monkeypatch.setattr("builtins.input", lambda prompt="": a.change_hash[:8])
    assert cli.main(["approve", a.proposal_id, "--no-apply", "--reason", "looks right"]) == 0
    got = store.proposal(a.proposal_id)
    assert got.status == "approved" and got.decided_by.startswith("cli:") and got.reason == "looks right"
    # --hash with the 12-char prefix the proposal text shows
    assert cli.main(["approve", b.proposal_id, "--hash", b.change_hash[:12], "--no-apply"]) == 0
    assert store.proposal(b.proposal_id).status == "approved"
    assert not Path(store.root).joinpath("..", "deploy", "docker-compose.override.yml").exists()


def test_cli_approve_hands_over_to_the_apply_engine(cli_env, monkeypatch, capsys):
    store = cli_env
    cli.main(["propose", EID, "--no-llm"])
    p = _pending(store)[0]
    seen = {}

    def fake_apply(settings, st, proposal_id, allow_protected=False, log=print, **deps):
        seen.update(pid=proposal_id, allow=allow_protected)
        q = st.proposal(proposal_id)
        for s in ("applying", "applied", "verified"):
            P.transition(q, s, "test")
        st.write_proposal(q)
        return q

    import faultline.remediation.apply as apply_mod
    monkeypatch.setattr(apply_mod, "apply_and_verify", fake_apply)
    assert cli.main(["approve", p.proposal_id, "--yes"]) == 0
    assert seen == {"pid": p.proposal_id, "allow": False}
    assert store.proposal(p.proposal_id).status == "verified"


def test_propose_after_analysis_never_breaks_the_run(cli_env, monkeypatch, settings):
    store = cli_env
    made = cli._propose_after_analysis(settings, store, [EID, "missing"], use_llm=False)
    assert made and all(p.status == "pending" for p in made)

    def boom(*a, **k):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(propose, "build_proposals", boom)
    assert cli._propose_after_analysis(settings, store, [EID], use_llm=False) == []
