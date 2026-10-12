"""LIVE integration tests for human-approved AI fixes, against the real Ditto stack (compose project `bosch-ditto`).

Skipped unless FAULTLINE_LIVE=1 AND Docker is reachable AND the configured compose project has containers, so the
normal suite stays Docker-free. They drive the real CLI (`python -m faultline ...`) as subprocesses and assert on
the reports/ blackboard, the Faultline override file and `docker inspect`.

    FAULTLINE_LIVE=1 .venv/bin/python -m pytest -q tests/integration -x -s

They take minutes (each apply recreates Ditto services, which need 60-120 s to rejoin the cluster). Every test
restores the stack in a `finally`: STOP cleared, the override back to its pre-test bytes (absent if it was absent),
any service whose compose config-hash moved recreated from the stack's own compose files, then a wait until every
container is running/healthy and the Things API answers 200. The stack's original state is captured once per module
and the module ends by asserting the stack is back to it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").is_file() else sys.executable
# Services with a Docker healthcheck that must be `healthy`; everything else in the project must be `running`.
DITTO_SERVICES = ("policies", "things", "things-search", "connectivity", "gateway")
HEALTHCHECK_FIX = "add_or_tighten_healthcheck"
BASELINE_MAX_AGE_S = 6 * 3600
# (target, failure) attempts for test 1, in order, until one yields an add_or_tighten_healthcheck proposal.
EXPERIMENT_ATTEMPTS = (("things", "container_crash"), ("policies", "container_stop"))


# ---------------------------------------------------------------------- skip gate
def _live_reason() -> Optional[str]:
    """None when the live tests may run, else why they are skipped. Touches Docker only when FAULTLINE_LIVE=1."""
    if os.environ.get("FAULTLINE_LIVE", "").strip() != "1":
        return "live Docker test: set FAULTLINE_LIVE=1 to run against the real Ditto stack"
    if shutil.which("docker") is None:
        return "docker CLI not found"
    try:
        from faultline.config import load_settings
        project = load_settings().compose_project
        res = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                             capture_output=True, text=True, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return f"docker not reachable: {exc}"
    if res.returncode != 0:
        return f"docker not reachable: {res.stderr.strip()[:200]}"
    if not res.stdout.strip():
        return f"compose project {project} has no running containers"
    return None


_REASON = _live_reason()
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


# ---------------------------------------------------------------------- helpers
def _settings():
    from faultline.config import load_settings
    return load_settings()


def _store(s=None):
    from faultline.reporting.store import ReportStore
    return ReportStore((s or _settings()).reports_dir)


def cli(*args: str, timeout: float = 900, env: Optional[dict] = None, check: bool = False) -> subprocess.CompletedProcess:
    """Run `python -m faultline <args>` from the repo root, echo its output (visible with -s)."""
    full_env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
    full_env.pop("FAULTLINE_LIVE", None)
    t0 = time.time()
    print(f"\n$ faultline {' '.join(args)}", flush=True)
    res = subprocess.run([PY, "-m", "faultline", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                         env=full_env)
    out = (res.stdout or "") + (res.stderr or "")
    print(out.rstrip()[-6000:], flush=True)
    print(f"  -> exit {res.returncode} in {time.time() - t0:.0f}s", flush=True)
    if check:
        assert res.returncode == 0, f"faultline {' '.join(args)} exited {res.returncode}:\n{out[-3000:]}"
    return res


def sha256_of(path: str | Path) -> Optional[str]:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def file_bytes(path: str | Path) -> Optional[bytes]:
    p = Path(path)
    return p.read_bytes() if p.is_file() else None


def project_containers(project: str) -> dict[str, dict]:
    """service -> docker inspect dict, for every container of the compose project."""
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
                         capture_output=True, text=True, timeout=30).stdout.split()
    if not ids:
        return {}
    data = json.loads(subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, timeout=60).stdout)
    out = {}
    for c in data:
        svc = (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service")
        if svc:
            out[svc] = c
    return out


def snapshot(project: str) -> dict[str, dict]:
    """The comparable identity of every container: id, Created, config-hash, config_files, healthcheck test."""
    snap = {}
    for svc, c in project_containers(project).items():
        labels = c["Config"].get("Labels") or {}
        snap[svc] = {
            "id": c["Id"][:12],
            "created": c["Created"],
            "config_hash": labels.get("com.docker.compose.config-hash"),
            "config_files": labels.get("com.docker.compose.project.config_files"),
            "healthcheck": (c["Config"].get("Healthcheck") or {}).get("Test"),
        }
    return snap


def healthcheck_test(project: str, svc: str) -> list:
    c = project_containers(project).get(svc) or {}
    return list(((c.get("Config") or {}).get("Healthcheck") or {}).get("Test") or [])


def api_ok(s) -> bool:
    import requests
    try:
        r = requests.get(f"{s.ditto_url}/api/2/things", params={"limit": 1}, auth=(s.ditto_user, s.ditto_password),
                         timeout=5)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def stack_problems(s) -> list[str]:
    problems = []
    conts = project_containers(s.compose_project)
    for svc, c in sorted(conts.items()):
        st = c.get("State") or {}
        if st.get("Status") != "running" or st.get("Paused"):
            problems.append(f"{svc} {st.get('Status')}{' paused' if st.get('Paused') else ''}")
            continue
        health = (st.get("Health") or {}).get("Status")
        if health is not None and health != "healthy":
            problems.append(f"{svc} {health}")
    for svc in DITTO_SERVICES:
        if svc not in conts:
            problems.append(f"{svc} missing")
    if not api_ok(s):
        problems.append("Things API not 200")
    return problems


def wait_stack_healthy(s, timeout: float = 600, label: str = "") -> None:
    """Every container running (healthy where it has a healthcheck) and the API 200, 3 checks in a row."""
    t0, good, last = time.time(), 0, []
    while time.time() - t0 < timeout:
        last = stack_problems(s)
        good = 0 if last else good + 1
        if good >= 3:
            print(f"  stack healthy{' (' + label + ')' if label else ''} after {time.time() - t0:.0f}s", flush=True)
            return
        time.sleep(5)
    raise AssertionError(f"stack not healthy after {timeout:.0f}s{' (' + label + ')' if label else ''}: {last}")


def restore_stack(s, orig: dict, label: str = "") -> list[str]:
    """Put the stack back to `orig` (see the `original` fixture). Returns what it did. Never deletes volumes or
    other projects' containers; only `docker compose up -d --no-deps --force-recreate <svc>` on our project."""
    from faultline.remediation import override as ov
    store, did = _store(s), []
    store.clear_stop()
    path = Path(s.remediation_override_file)
    want = orig["override_bytes"]
    if file_bytes(path) != want:
        if want is None:
            path.unlink(missing_ok=True)
            did.append("removed the override (it did not exist before)")
        else:
            ov.write_bytes_atomic(path, want)
            did.append("restored the override bytes")
    for f, name in ((store.lock_file, "ACTIVE_EXPERIMENT.json"), (store.fix_lock_file, "FIX_IN_FLIGHT.json")):
        if f.exists() and not orig.get(name):
            f.unlink(missing_ok=True)
            did.append(f"removed leftover {name}")
    now = snapshot(s.compose_project)
    moved = [svc for svc, o in orig["containers"].items()
             if svc in now and (now[svc]["config_hash"] != o["config_hash"]
                                or now[svc]["config_files"] != o["config_files"]
                                or now[svc]["healthcheck"] != o["healthcheck"])]
    if moved:
        ok, out = ov.recreate(s, moved)
        did.append(f"recreated {', '.join(moved)} from the base compose files (ok={ok})")
        print(f"  [restore] recreate {moved}: ok={ok}\n{out[-1500:]}", flush=True)
        assert ok, f"recreating {moved} failed: {out[-1500:]}"
        # the same proxy refresh the apply engine does after a recreate (nginx caches the old container IPs)
        from types import SimpleNamespace
        from faultline.chaos.docker_ctl import DockerController
        from faultline.remediation import apply as A
        deps = SimpleNamespace(topology=store.topology(), docker=DockerController(s.compose_project, s.compose_network))
        reloaded = A._refresh_proxies(deps, moved, lambda m: print(f"  [restore] {m}", flush=True))
        if reloaded:
            did.append(f"reloaded {', '.join(reloaded)}")
    wait_stack_healthy(s, 600, label or "restore")
    print(f"  [restore] {'; '.join(did) or 'nothing to restore'}", flush=True)
    return did


def assert_stack_original(s, orig: dict) -> None:
    store = _store(s)
    assert file_bytes(s.remediation_override_file) == orig["override_bytes"], "override not back to its original bytes"
    assert not store.stop_requested(), "STOP still present"
    assert not store.lock_file.exists(), "ACTIVE_EXPERIMENT.json still present"
    assert not store.fix_lock_file.exists(), "FIX_IN_FLIGHT.json still present"
    now = snapshot(s.compose_project)
    for svc, o in orig["containers"].items():
        assert svc in now, f"{svc} is gone"
        for k in ("config_hash", "config_files", "healthcheck"):
            assert now[svc][k] == o[k], f"{svc} {k}: {now[svc][k]!r} != original {o[k]!r}"
    assert stack_problems(s) == []


def proposals_for(store, eid: str) -> list:
    return [p for p in store.proposals() if p.experiment_id == eid]


def audit_statuses(p) -> list[str]:
    """The status path: the initial `pending` plus every real transition (notes repeat the status; skipped)."""
    return [e.to_status for e in p.audit if e.from_status is None or e.from_status != e.to_status]


def show(p) -> None:
    print(f"\n  proposal {p.proposal_id}: {p.catalog_fix_id} on {p.target_services} -> {p.status}", flush=True)
    for e in p.audit:
        print(f"    {e.at:%H:%M:%S} {e.actor:<16} {e.from_status or '-'} -> {e.to_status}  {e.note[:400]}", flush=True)
    if p.verification:
        v = p.verification
        print(f"    verification: {v.outcome} rerun={v.rerun_experiment_id} targeted={v.targeted_metric} "
              f"reason={v.reason}", flush=True)
        for c in v.comparisons:
            print(f"      {c.metric}: {c.before} -> {c.after} ({c.verdict})", flush=True)


def ensure_baseline(s) -> None:
    f = Path(s.reports_dir) / "baseline.json"
    if f.is_file() and time.time() - f.stat().st_mtime < BASELINE_MAX_AGE_S:
        print(f"  baseline is fresh ({(time.time() - f.stat().st_mtime) / 60:.0f} min old)", flush=True)
        return
    cli("baseline", "--duration", "30", timeout=300, check=True)
    assert f.is_file()


def make_proposal(s, eid: str, fix_id: str, services: list[str], finding_code: str, include_test: bool = False):
    """A pending proposal for exactly `services`, built through the same code path propose.build_proposals uses
    (catalog build against the live base config + the current override, catalog.preview, change_hash). Used where
    the CLI would pick other targets (the test-only entry on `things`) or where an equal proposal is blocked."""
    from faultline.remediation import catalog
    from faultline.remediation import proposals as P
    from faultline.remediation import propose
    from faultline.schemas import Finding, FixProposal, ProposalAuditEntry

    store = _store(s)
    report = store.experiment(eid)
    assert report is not None, f"no experiment {eid}"
    fix = catalog.get(fix_id, include_test=include_test)
    assert fix is not None, f"catalog has no {fix_id}"
    base = propose.base_compose(s)
    current, text = propose._current_override(s)
    topo = propose._topology(s, store)
    change = fix.build(copy.deepcopy(base), copy.deepcopy(current), list(services), topo)
    assert change is not None, f"{fix_id} produces no change for {services}"
    label = propose._path_label(s)
    preview = catalog.preview(change, text, label)
    finding = next((f for f in report.findings if f.code == finding_code),
                   Finding(code=finding_code, severity="high", title=f"{services[0]} (integration test)", evidence=""))
    cand = propose.Candidate(finding=finding, fix=fix, change=change, services=list(change.services),
                             preview=preview, path_label=label,
                             requires_allow_protected=propose._requires_allow_protected(s, fix, list(change.services)))
    metrics = propose.expected_metrics(report, change, cand.services, store)
    p = FixProposal(
        proposal_id=P.new_proposal_id(), experiment_id=eid, finding_code=finding_code, catalog_fix_id=fix_id,
        target_services=cand.services, change_preview=preview, change_hash=P.change_hash(preview),
        explanation=propose.template_explanation(report, cand, metrics), expected_metrics=metrics,
        requires_allow_protected=cand.requires_allow_protected, rank=1, ranked_by="code", status="pending",
        expires_at=P.expires_at(s.fix_proposal_timeout_s),
        audit=[ProposalAuditEntry(actor="faultline", from_status=None, to_status="pending",
                                  note="proposed by the live integration test")],
    )
    store.write_proposal(p)
    return p


# ---------------------------------------------------------------------- fixtures
STATE: dict = {}      # shared between the tests of this module: the experiment id test 1 produced


@pytest.fixture(scope="module")
def s():
    return _settings()


@pytest.fixture(scope="module")
def original(s):
    """The stack as it was before any test, and a final check that the module leaves it exactly so."""
    store = _store(s)
    assert not store.stop_requested(), "STOP is present before the live tests; clear it (`faultline resume`) first"
    assert not store.active(), f"an experiment is active ({store.active()}); wait for it to finish"
    assert not store.fix_lock_file.exists(), "FIX_IN_FLIGHT.json is present; another fix is in flight"
    wait_stack_healthy(s, 600, "before the live tests")
    orig = {"override_bytes": file_bytes(s.remediation_override_file),
            "containers": snapshot(s.compose_project)}
    print(f"\n  original override: {'absent' if orig['override_bytes'] is None else sha256_of(s.remediation_override_file)}",
          flush=True)
    for svc in DITTO_SERVICES:
        o = orig["containers"].get(svc, {})
        print(f"  original {svc}: {o.get('id')} hash {str(o.get('config_hash'))[:12]} healthcheck {o.get('healthcheck')}",
              flush=True)
    yield orig
    try:
        restore_stack(s, orig, "end of module")
    finally:
        assert_stack_original(s, orig)
        print("  FINAL: stack back to its original configuration and healthy", flush=True)


@pytest.fixture
def clean_stack(s, original):
    """Each test starts from, and returns to, the original stack."""
    wait_stack_healthy(s, 600, "before test")
    yield original
    restore_stack(s, original, "after test")


def crash_experiment(s) -> str:
    """Test 1's experiment, else the newest container_crash/things experiment on disk with a non-info finding."""
    if STATE.get("eid"):
        return STATE["eid"]
    store = _store(s)
    cands = [r for r in (store.experiment(e) for e in store.experiment_ids()) if r is not None
             and r.failure_type == "container_crash" and r.target.get("service") == "things"
             and any(f.severity != "info" for f in r.findings)]
    assert cands, "no container_crash experiment on things to attach proposals to; run test 1 first"
    return max(cands, key=lambda r: r.started_at).experiment_id


# ---------------------------------------------------------------------- 1. approve -> apply -> re-run -> verdict
def test_1_full_approve_flow(s, clean_stack):
    store = _store(s)
    ovr = Path(s.remediation_override_file)
    ensure_baseline(s)

    chosen = eid = None
    for target, failure in EXPERIMENT_ATTEMPTS:
        before_ids = set(store.experiment_ids())
        cli("run", "--target", target, "--failure", failure, "--param", "duration_s=20", "--no-llm", "--analyze",
            "--no-approvals", timeout=900, check=True)
        new = sorted(set(store.experiment_ids()) - before_ids)
        assert len(new) == 1, f"expected one new experiment, got {new}"
        eid = new[0]
        rep = store.experiment(eid)
        print(f"  experiment {eid}: {rep.status}, findings "
              f"{[(f.code, f.severity) for f in rep.findings]}", flush=True)
        assert rep.status != "skipped", f"experiment skipped: {rep.error}"
        hc = [p for p in proposals_for(store, eid) if p.catalog_fix_id == HEALTHCHECK_FIX and p.status == "pending"]
        if not hc:
            cli("propose", eid, "--no-llm", timeout=300, check=True)
            hc = [p for p in proposals_for(store, eid) if p.catalog_fix_id == HEALTHCHECK_FIX and p.status == "pending"]
        if hc:
            chosen = hc[0]
            break
        print(f"  no {HEALTHCHECK_FIX} proposal for {eid}; trying the next experiment", flush=True)
        wait_stack_healthy(s, 600, "between experiments")
    assert chosen is not None, f"no experiment produced a {HEALTHCHECK_FIX} proposal (last {eid})"
    STATE["eid"] = eid
    print(f"  all proposals for {eid}: {[(p.proposal_id, p.catalog_fix_id, p.target_services, p.status) for p in proposals_for(store, eid)]}",
          flush=True)
    show(chosen)
    assert chosen.change_hash == hashlib.sha256(chosen.change_preview.encode()).hexdigest()
    targets = list(chosen.target_services)

    # approve with the hash shown; this applies, re-runs the experiment and keeps or rolls back the fix
    wait_stack_healthy(s, 600, "before approve")
    override_before = file_bytes(ovr)
    snap_before = snapshot(s.compose_project)
    res = cli("approve", chosen.proposal_id, "--hash", chosen.change_hash, "--reason", "integration test", timeout=1800)
    p = store.proposal(chosen.proposal_id)
    show(p)
    STATE["test1"] = {"proposal": p.proposal_id, "status": p.status, "eid": eid, "targets": targets}

    assert p.status in ("verified", "rolled_back"), f"not final: {p.status}"
    statuses = audit_statuses(p)
    assert statuses[:4] == ["pending", "approved", "applying", "applied"] or \
        statuses[:4] == ["pending", "approved", "applying", "rolled_back"], statuses
    approved_entry = next(e for e in p.audit if e.to_status == "approved")
    assert approved_entry.actor.startswith("cli:") and "integration test" in approved_entry.note
    assert p.decided_by and p.reason == "integration test"
    assert p.override_backup and Path(p.override_backup).is_file(), "no override backup recorded"
    if override_before is None:
        assert Path(p.override_backup).read_bytes().startswith(b"# faultline: override did not exist")

    if "applied" in statuses:
        assert p.applied_at is not None
        v = p.verification
        assert v is not None, "applied but no verification recorded"
        assert v.rerun_experiment_id, f"no re-run experiment id ({v.reason})"
        assert (Path(s.reports_dir) / "experiments" / f"{v.rerun_experiment_id}.json").is_file()
        rerun = store.experiment(v.rerun_experiment_id)
        orig_rep = store.experiment(eid)
        assert rerun.failure_type == orig_rep.failure_type and rerun.target == orig_rep.target
        if v.outcome == "verified" or p.status == "verified":
            metrics = {c.metric for c in v.comparisons}
            assert {"time_to_recover_s", "error_rate", "p95_ms", "service_score", f"finding:{p.finding_code}"} <= metrics
    if p.status == "verified":
        assert res.returncode == 0
        assert p.verification.outcome == "verified" and p.verification.reason
        text = ovr.read_text()
        assert HEALTHCHECK_FIX in text
        for svc in targets:
            assert f"  {svc}:" in text
            test = healthcheck_test(s.compose_project, svc)
            print(f"  live {svc} healthcheck: {test}", flush=True)
            assert any("/ready" in str(x) for x in test), f"{svc} healthcheck is {test}"
            assert snapshot(s.compose_project)[svc]["config_hash"] != snap_before[svc]["config_hash"]
    else:  # rolled_back: a recorded reason, the override back byte-for-byte, the healthchecks back to /alive
        assert res.returncode != 0
        assert p.audit[-1].to_status == "rolled_back" and p.audit[-1].note, "rolled back without a reason"
        if "verification_failed" in statuses:
            assert p.verification and p.verification.comparisons and p.verification.reason
        assert file_bytes(ovr) == override_before
        for svc in targets:
            assert any("/alive" in str(x) for x in healthcheck_test(s.compose_project, svc))
    assert not store.fix_lock_file.exists(), "FIX_IN_FLIGHT.json left behind"
    assert not store.active(), "ACTIVE_EXPERIMENT.json left behind"


# ---------------------------------------------------------------------- 2. reject -> nothing changes
def test_2_reject_changes_nothing(s, clean_stack):
    store = _store(s)
    eid = crash_experiment(s)
    pending = [p for p in proposals_for(store, eid) if p.status == "pending"]
    if not pending:
        cli("propose", eid, "--no-llm", timeout=300)
        pending = [p for p in proposals_for(store, eid) if p.status == "pending"]
    if not pending:   # everything for this experiment is already decided/blocked: build an equal one directly
        rep = store.experiment(eid)
        code = next(f.code for f in rep.findings if f.code in ("dependency_blind_readiness", "health_check_masked_failure"))
        pending = [make_proposal(s, eid, HEALTHCHECK_FIX, ["things"], code)]
    p = pending[0]
    show(p)
    ovr = Path(s.remediation_override_file)
    sha_before, exists_before = sha256_of(ovr), ovr.exists()
    snap_before = snapshot(s.compose_project)

    cli("reject", p.proposal_id, "--reason", "integration test", timeout=120, check=True)
    time.sleep(3)
    p2 = store.proposal(p.proposal_id)
    show(p2)
    STATE["test2"] = {"proposal": p2.proposal_id, "fix": p2.catalog_fix_id, "status": p2.status,
                      "sha_before": sha_before, "sha_after": sha256_of(ovr)}
    assert p2.status == "rejected" and p2.reason == "integration test"
    assert audit_statuses(p2)[-1] == "rejected" and p2.audit[-1].actor.startswith("cli:")
    assert p2.override_backup is None and p2.applied_at is None and p2.verification is None
    assert ovr.exists() == exists_before and sha256_of(ovr) == sha_before
    snap_after = snapshot(s.compose_project)
    for svc in set(p2.target_services) | set(DITTO_SERVICES):
        assert snap_after[svc]["id"] == snap_before[svc]["id"], f"{svc} was recreated"
        assert snap_after[svc]["created"] == snap_before[svc]["created"], f"{svc} was recreated"
    # a rejected proposal can never be approved afterwards
    res = cli("approve", p2.proposal_id, "--hash", p2.change_hash, "--reason", "should be refused", timeout=120)
    assert res.returncode != 0 and store.proposal(p2.proposal_id).status == "rejected"
    assert sha256_of(ovr) == sha_before


# ---------------------------------------------------------------------- 3. a bad fix is rolled back automatically
def test_3_bad_fix_rolls_back(s, clean_stack):
    store = _store(s)
    eid = crash_experiment(s)
    ovr = Path(s.remediation_override_file)
    override_before = file_bytes(ovr)
    orig_hc = clean_stack["containers"]["things"]["healthcheck"]
    assert any("/alive" in str(x) for x in orig_hc), orig_hc
    os.environ["FAULTLINE_TEST_CATALOG"] = "1"     # for make_proposal's catalog lookup in this process only
    try:
        p = make_proposal(s, eid, "break_healthcheck_for_test", ["things"], "health_check_masked_failure",
                          include_test=True)
    finally:
        os.environ.pop("FAULTLINE_TEST_CATALOG", None)
    show(p)
    assert p.target_services == ["things"] and "exit 1" in p.change_preview

    # without the test-catalog switch the engine must refuse the test-only fix (nothing touched)
    res = cli("approve", p.proposal_id, "--hash", p.change_hash, "--reason", "integration test (no test catalog)",
              timeout=300)
    p0 = store.proposal(p.proposal_id)
    show(p0)
    assert p0.status == "approved" and "test-only" in p0.audit[-1].note and res.returncode != 0
    assert file_bytes(ovr) == override_before
    # an approved proposal cannot be approved again; use a fresh one for the real run
    p = make_proposal(s, eid, "break_healthcheck_for_test", ["things"], "health_check_masked_failure",
                      include_test=True)

    wait_stack_healthy(s, 600, "before bad fix")
    t0 = time.time()
    res = cli("approve", p.proposal_id, "--hash", p.change_hash, "--reason", "integration test", timeout=1800,
              env={"FAULTLINE_TEST_CATALOG": "1"})
    p = store.proposal(p.proposal_id)
    show(p)
    statuses = audit_statuses(p)
    STATE["test3"] = {"proposal": p.proposal_id, "status": p.status, "reason": p.audit[-1].note,
                      "seconds": round(time.time() - t0)}
    assert res.returncode != 0
    assert p.status == "rolled_back", statuses
    assert statuses[:4] == ["pending", "approved", "applying", "rolled_back"], statuses
    reason = p.audit[-1].note
    assert "unhealthy" in reason or "health" in reason, reason
    assert "override restored" in reason, reason
    assert p.verification is None, "a fix rolled back before the re-run must not carry a verification"
    assert p.override_backup and Path(p.override_backup).is_file()
    assert file_bytes(ovr) == override_before, "override not restored byte-for-byte"
    assert not store.fix_lock_file.exists() and not store.active()

    wait_stack_healthy(s, 600, "after bad-fix rollback")
    test = healthcheck_test(s.compose_project, "things")
    print(f"  things healthcheck after rollback: {test}", flush=True)
    assert test == orig_hc
    snap = snapshot(s.compose_project)["things"]
    assert snap["config_hash"] == clean_stack["containers"]["things"]["config_hash"]
    assert snap["config_files"] == clean_stack["containers"]["things"]["config_files"]
    assert stack_problems(s) == []


# ---------------------------------------------------------------------- 4. STOP -> apply refused
def test_4_stop_refuses_apply(s, clean_stack):
    store = _store(s)
    eid = crash_experiment(s)
    ovr = Path(s.remediation_override_file)
    sha_before = sha256_of(ovr)
    snap_before = snapshot(s.compose_project)
    rep = store.experiment(eid)
    code = next((f.code for f in rep.findings if f.code in ("dependency_blind_readiness", "health_check_masked_failure")),
                "health_check_masked_failure")
    p = make_proposal(s, eid, HEALTHCHECK_FIX, ["things"], code)
    show(p)
    assert not p.requires_allow_protected
    try:
        cli("stop", timeout=60, check=True)
        assert store.stop_requested()
        res = cli("approve", p.proposal_id, "--hash", p.change_hash, "--reason", "integration test", timeout=600)
        p2 = store.proposal(p.proposal_id)
        show(p2)
        refusal = p2.audit[-1].note
        STATE["test4"] = {"proposal": p2.proposal_id, "status": p2.status, "refusal": refusal}
        assert res.returncode != 0
        assert p2.status == "approved", p2.status
        assert audit_statuses(p2) == ["pending", "approved"]          # the refusal is a note, not a transition
        assert p2.audit[-1].from_status == p2.audit[-1].to_status == "approved"
        assert refusal.startswith("apply refused") and "STOP" in refusal, refusal
        assert p2.override_backup is None and p2.applied_at is None
        assert sha256_of(ovr) == sha_before
        snap_after = snapshot(s.compose_project)
        for svc in DITTO_SERVICES:
            assert snap_after[svc]["created"] == snap_before[svc]["created"], f"{svc} was recreated"
        assert not store.fix_lock_file.exists() and not store.active()
    finally:
        cli("resume", timeout=60)
    assert not store.stop_requested()
