from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import Settings, load_settings
from .reporting.store import ReportStore


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------- shared wiring
class Session:
    """Everything a live command needs: docker, topology, the two probes, policy, thresholds."""

    def __init__(self, s: Settings, store: ReportStore, need_probes: bool = True, rebuild_topology: bool = False):
        from .chaos.docker_ctl import DockerController
        from .observe.probes import HealthProbe, ThingsApiProbe
        from .observe.window import SampleWindow
        from .planning.planner import Policy
        from .safety.thresholds import load_policy, thresholds_from

        self.s = s
        self.store = store
        self.docker = DockerController(s.compose_project, s.compose_network)
        self.topology = None if rebuild_topology else store.topology()
        if self.topology is None:
            self.topology = build_topology(s, store, live=True)
        self.policy_raw = load_policy(s.thresholds_file)
        self.policy = Policy(self.policy_raw)
        base = store.baseline()
        self.thresholds = thresholds_from(self.policy_raw, base.api.p95_ms if base else None)
        self.window = SampleWindow()
        self.api = self.health = None
        if need_probes:
            self.api = ThingsApiProbe(s.ditto_url, s.ditto_user, s.ditto_password, self.window, s.probe_rps, s.probe_namespace, log=say)
            self.health = HealthProbe(self.docker, self.topology, s.health_interval_s, log=lambda m: None)
            self.api.start()
            self.health.start()

    def warmup(self, seconds: float) -> None:
        say(f"warming up probes for {seconds:.0f}s ...")
        time.sleep(seconds)
        st = self.window.stats_since(time.time() - seconds)
        say(f"  steady state: {st.sample_count} requests, success {100 * (st.success_rate or 0):.1f}%, p95 {st.p95_ms} ms")

    def close(self) -> None:
        for p in (self.api, self.health):
            if p:
                p.stop()


def build_topology(s: Settings, store: ReportStore, live: bool):
    from .topology.graph import build
    from .topology.render import markdown

    nginx = s.nginx_conf if Path(s.nginx_conf).is_file() else None
    topo = build(s.compose_file, s.compose_project, nginx, s.knowledge_file, live=live)
    store.write_topology(topo)
    (store.root / "topology.md").write_text(markdown(topo))
    return topo


def llm_client(s: Settings, enabled: bool = True):
    from .planning.llm import client_from_settings

    c = client_from_settings(s)
    c.enabled = c.enabled and enabled
    return c


def parse_params(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------- commands
def cmd_setup(s, store, a):
    from .chaos.docker_ctl import DockerController

    ok = True
    try:
        d = DockerController(s.compose_project, s.compose_network)
        cs = d.containers()
        say(f"docker: ok, project '{s.compose_project}' has {len(cs)} containers "
            f"({sum(1 for c in cs.values() if c.status == 'running')} running)")
        if not cs:
            say("  -> no containers. Start the stack: scripts/up.sh (or set FAULTLINE_COMPOSE_PROJECT in .env)")
            ok = False
        for img in (s.pumba_image, s.tc_image):
            say(f"image {img}: {'present' if d.has_image(img) else ('pulled' if d.pull(img) else 'UNAVAILABLE (network chaos disabled)')}")
    except Exception as exc:
        say(f"docker: FAILED ({exc})")
        ok = False
    import requests
    try:
        r = requests.get(f"{s.ditto_url}/health", timeout=5)
        say(f"ditto {s.ditto_url}/health: HTTP {r.status_code} {r.text.strip()[:80]}")
        r = requests.get(f"{s.ditto_url}/api/2/things/{s.probe_namespace}:canary", auth=(s.ditto_user, s.ditto_password), timeout=5)
        say(f"ditto API auth as {s.ditto_user}: HTTP {r.status_code} ({'ok' if r.status_code in (200, 404) else 'check DITTO_USER/DITTO_PASSWORD'})")
    except requests.RequestException as exc:
        say(f"ditto: unreachable ({exc})")
        ok = False
    try:
        r = requests.get(f"{s.prometheus_url}/-/ready", timeout=3)
        say(f"prometheus {s.prometheus_url}: HTTP {r.status_code}")
    except requests.RequestException:
        say(f"prometheus {s.prometheus_url}: not reachable (optional)")
    c = llm_client(s)
    say(f"llm {c.describe()}: {'available' if c.available() else 'NOT available (' + (c.last_error or 'disabled') + ') -> deterministic planning'}")
    say("setup " + ("complete" if ok else "incomplete"))
    return 0 if ok else 1


def cmd_topology(s, store, a):
    from .topology.graph import summary_lines

    topo = build_topology(s, store, live=not a.no_live)
    say("\n".join(summary_lines(topo)))
    for f in topo.findings:
        say(f"finding: {f}")
    say(f"wrote {store.root / 'topology.json'} and topology.md")
    return 0


def cmd_baseline(s, store, a):
    from .observe.baseline import capture, to_markdown

    sess = Session(s, store)
    try:
        sess.warmup(10)
        b = capture(sess.api, sess.health, sess.window, a.duration, s.ditto_url, sess.policy_raw, log=say)
        store.write_baseline(b)
        (store.root / "baseline.md").write_text(to_markdown(b))
        say(f"baseline: success {100 * (b.api.success_rate or 0):.2f}%, p95 {b.api.p95_ms} ms -> abort at error>{b.thresholds.abort_error_rate_above:.0%} "
            f"or p95>{b.thresholds.abort_p95_ms_above:.0f} ms")
        for n in b.notes:
            say(f"  note: {n}")
    finally:
        sess.close()
    return 0


def cmd_catalog(s, store, a):
    from .chaos.catalog import CATALOG

    for ft in CATALOG.values():
        params = ", ".join(f"{p.name}={p.default}" + (f" [{p.lo}-{p.hi}]" if p.lo is not None else "") for p in ft.params)
        say(f"{ft.key:<18} {ft.title:<28} ({ft.slide_category}) roles={','.join(ft.roles)} {'degrade-only' if ft.degrade_only else 'outage'}\n"
            f"{'':<18} {ft.description}\n{'':<18} params: {params}")
    return 0


def _planner(sess: Session, use_llm: bool, allow_protected: bool):
    from .planning.planner import Planner

    images = {k for k, img in (("pumba", sess.s.pumba_image), ("tc", sess.s.tc_image)) if sess.docker.has_image(img)}
    return Planner(sess.topology, sess.policy, sess.store.experiments(), llm_client(sess.s, use_llm), sess.s, images, allow_protected)


def cmd_plan(s, store, a):
    from .planning.planner import describe_candidates

    sess = Session(s, store, need_probes=False)
    p = _planner(sess, not a.no_llm, a.allow_protected)
    say(describe_candidates(p.candidates()))
    spec = p.plan_next()
    if spec:
        say(f"\nnext experiment ({spec.planned_by}): {spec.failure_type} on {spec.target_service} {spec.parameters}")
        say(f"  why: {spec.rationale}\n  hypothesis: {spec.hypothesis.statement}")
        for n in spec.planner_notes:
            say(f"  note: {n}")
    return 0


def cmd_run(s, store, a):
    from .agents.injector import Injector

    if a.resume_stop:
        store.clear_stop()
    sess = Session(s, store)
    try:
        if store.baseline() is None:
            say("no baseline yet; run `faultline baseline` first for calibrated thresholds (using floors for now)")
        planner = _planner(sess, not a.no_llm, a.allow_protected)
        if planner.llm and planner.llm.available():
            # loading a local model costs RAM; do it before we measure steady state, not during it
            say(f"pre-warming LLM {planner.llm.describe()} ...")
            planner.llm.complete("Reply with OK.", "ping", max_tokens=5)
        sess.warmup(max(sess.policy.pre_window_s, 15))
        inj = Injector(s, store, sess.topology, sess.docker, sess.window, sess.api, sess.health, sess.thresholds, sess.policy, log=say)
        n = 1 if a.target else min(a.experiments, sess.policy.max_experiments)
        for i in range(n):
            if store.stop_requested():
                say("STOP requested; not starting another experiment")
                break
            if a.target:
                params = parse_params(a.param)
                if a.duration:
                    params["duration_s"] = a.duration
                spec = planner.plan_operator(a.target, a.failure, params)
            else:
                spec = planner.plan_next()
                if spec is None:
                    say("nothing left to test under the current policy")
                    break
                if a.duration:
                    spec.parameters["duration_s"] = a.duration
            say(f"\n=== experiment {i + 1}/{n}: {spec.failure_type} on {spec.target_service} {spec.parameters} [{spec.planned_by}]")
            say(f"    why: {spec.rationale}")
            say(f"    hypothesis: {spec.hypothesis.statement}")
            r = inj.run(spec)
            say(f"--- {r.status}: success during {r.during.success_rate}, recovered in {r.recovery.time_to_recover_s}s, "
                f"hypothesis {'held' if r.hypothesis_result.held else 'violated' if r.hypothesis_result.held is False else '-'}, "
                f"findings {[f.code for f in r.findings]}")
            planner.history.append(r)
            if i < n - 1:
                say(f"pause {sess.policy.pause_between_s:.0f}s so the system settles before the next experiment")
                time.sleep(sess.policy.pause_between_s)
        if a.analyze:
            _post_analysis(s, store, sess.topology, not a.no_llm)
    finally:
        sess.close()
    return 0


def _post_analysis(s, store, topology, use_llm: bool):
    from .agents import fix, risk, scorecard

    llm = llm_client(s, use_llm)
    risk.run(store, llm, topology, log=say)
    fix.run(store, llm, log=say)
    scorecard.run(store, topology, s.compose_project, log=say)


def cmd_rerun(s, store, a):
    prev = store.experiment(a.experiment_id)
    if not prev:
        say(f"no experiment {a.experiment_id}")
        return 1
    a.target, a.failure, a.param, a.duration = prev.target["service"], prev.failure_type, [f"{k}={v}" for k, v in prev.parameters.items()], None
    return cmd_run(s, store, a)


def cmd_risk(s, store, a):
    from .agents import risk

    risk.run(store, llm_client(s, not a.no_llm), store.topology(), only=a.ids or None, force=a.force, log=say)
    return 0


def cmd_fix(s, store, a):
    from .agents import fix

    fix.run(store, llm_client(s, not a.no_llm), only=a.ids or None, force=a.force, log=say)
    return 0


def cmd_scorecard(s, store, a):
    from .agents import scorecard

    sc = scorecard.run(store, store.topology(), s.compose_project, log=say)
    say((store.root / "scorecard.md").read_text())
    return 0


def cmd_narrate(s, store, a):
    from .agents import narrator

    narrator.run(store, llm_client(s, a.llm), a.llm, a.watch, log=say)
    return 0


def cmd_guardian(s, store, a):
    from .agents import guardian
    from .chaos.docker_ctl import DockerController

    guardian.run(s, store, DockerController(s.compose_project, s.compose_network), a.interval, log=say)
    return 0


def cmd_heal(s, store, a):
    """Operator reset between demos: remove chaos leftovers, restart the Ditto cluster in dependency order, wait for UP."""
    import requests
    from .chaos.actions import clear_qdisc
    from .chaos.docker_ctl import DockerController

    d = DockerController(s.compose_project, s.compose_network)
    store.clear_active()
    for h in d.helper_containers():
        d.remove_helper(h)
        say(f"removed helper {h.name}")
    containers = d.containers()
    for svc, c in containers.items():
        c.reload()
        if not d.is_connected(svc):
            d.connect(svc, [svc]); say(f"reconnected {svc}")
        if c.attrs["State"].get("Paused"):
            c.unpause(); say(f"unpaused {svc}")
        elif c.status != "running":
            c.start(); say(f"started {svc}")
        if svc in ("mongodb", "policies", "things", "things-search", "connectivity", "gateway", "nginx") and clear_qdisc(d, s.tc_image, c.name):
            say(f"removed netem qdisc on {svc}")
    order = [x for x in ("mongodb", "policies", "things", "things-search", "connectivity", "gateway") if x in containers]
    if a.restart:
        for svc in order:
            say(f"restarting {svc} ...")
            d.restart(svc, timeout=20)
            time.sleep(5 if svc != "mongodb" else 10)
    deadline = time.time() + 600
    say(f"waiting for {s.ditto_url}/health = UP ...")
    while time.time() < deadline:
        try:
            r = requests.get(f"{s.ditto_url}/health", timeout=5)
            if r.ok and r.json().get("status") == "UP":
                say("stack healthy")
                return 0
        except (requests.RequestException, ValueError):
            pass
        time.sleep(5)
    say("stack still not healthy after 10 minutes; inspect `faultline status` and container logs")
    return 1


def cmd_stop(s, store, a):
    store.request_stop("cli")
    say("STOP requested: the running experiment rolls back within ~1s; the guardian restores anything still down. `faultline resume` clears it.")
    return 0


def cmd_resume(s, store, a):
    store.clear_stop()
    say("STOP cleared")
    return 0


def cmd_status(s, store, a):
    from .chaos.docker_ctl import DockerController

    d = DockerController(s.compose_project, s.compose_network)
    for svc, c in sorted(d.containers().items()):
        st = d.state(svc)
        say(f"{svc:<16} {st.get('status'):<10} {st.get('health') or '-':<10} restarts={st.get('restart_count')}")
    say(f"stop requested: {store.stop_requested()}  active: {(store.active() or {}).get('experiment_id')}  "
        f"experiments: {len(store.experiment_ids())}  guardian: {'alive' if (store.heartbeat('guardian') or {}).get('at', 0) > time.time() - 15 else 'offline'}")
    return 0


def cmd_dashboard(s, store, a):
    from .dashboard.server import serve

    serve(store, a.port)
    return 0


def cmd_demo(s, store, a):
    say("== 1/5 topology"); cmd_topology(s, store, argparse.Namespace(no_live=False))
    say("== 2/5 baseline"); cmd_baseline(s, store, argparse.Namespace(duration=a.baseline))
    say("== 3/5 experiments")
    a.analyze, a.target, a.failure, a.param, a.duration, a.resume_stop = True, None, None, [], None, True
    cmd_run(s, store, a)
    say("== 4/5 narrative")
    cmd_narrate(s, store, argparse.Namespace(llm=not a.no_llm, watch=False))
    say("== 5/5 done — open the dashboard: faultline dashboard")
    return 0


# ---------------------------------------------------------------------- parser
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="faultline", description="AI-driven chaos engineering for Eclipse Ditto")
    p.add_argument("--config", help="settings yaml (default config/settings.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="check Docker, the Ditto stack, helper images and the LLM").set_defaults(fn=cmd_setup)
    t = sub.add_parser("topology", help="map services, dependencies and criticality"); t.add_argument("--no-live", action="store_true"); t.set_defaults(fn=cmd_topology)
    b = sub.add_parser("baseline", help="measure steady state and derive safety thresholds"); b.add_argument("--duration", type=int, default=60); b.set_defaults(fn=cmd_baseline)
    sub.add_parser("catalog", help="list failure types").set_defaults(fn=cmd_catalog)
    pl = sub.add_parser("plan", help="show ranked candidates and the next pick without running"); pl.add_argument("--no-llm", action="store_true"); pl.add_argument("--allow-protected", action="store_true"); pl.set_defaults(fn=cmd_plan)

    r = sub.add_parser("run", help="run experiments (planner-chosen or operator-chosen)")
    r.add_argument("--experiments", type=int, default=1)
    r.add_argument("--target"); r.add_argument("--failure"); r.add_argument("--param", action="append", default=[], help="k=v, repeatable")
    r.add_argument("--duration", type=int); r.add_argument("--no-llm", action="store_true"); r.add_argument("--allow-protected", action="store_true")
    r.add_argument("--analyze", action="store_true", help="run risk, fix and scorecard agents afterwards")
    r.add_argument("--resume-stop", action="store_true", help="clear a leftover STOP file first")
    r.set_defaults(fn=cmd_run)

    rr = sub.add_parser("rerun", help="repeat a previous experiment by id"); rr.add_argument("experiment_id")
    for flag in ("--no-llm", "--allow-protected", "--analyze", "--resume-stop"):
        rr.add_argument(flag, action="store_true")
    rr.set_defaults(fn=cmd_rerun, experiments=1)

    for name, fn, hlp in (("risk", cmd_risk, "score severity of experiments"), ("fix", cmd_fix, "recommend fixes (advisory only)")):
        x = sub.add_parser(name, help=hlp); x.add_argument("ids", nargs="*"); x.add_argument("--force", action="store_true"); x.add_argument("--no-llm", action="store_true"); x.set_defaults(fn=fn)
    sub.add_parser("scorecard", help="ranked resilience score per service").set_defaults(fn=cmd_scorecard)
    n = sub.add_parser("narrate", help="plain-English play-by-play"); n.add_argument("--watch", action="store_true"); n.add_argument("--llm", action="store_true"); n.set_defaults(fn=cmd_narrate)
    g = sub.add_parser("guardian", help="independent safety process"); g.add_argument("--interval", type=float, default=5.0); g.set_defaults(fn=cmd_guardian)
    sub.add_parser("stop", help="KILL SWITCH: abort and roll back now").set_defaults(fn=cmd_stop)
    h = sub.add_parser("heal", help="reset the sandbox: remove chaos leftovers, (optionally) restart the cluster, wait for UP")
    h.add_argument("--restart", action="store_true", help="also restart the Ditto services in dependency order"); h.set_defaults(fn=cmd_heal)
    sub.add_parser("resume", help="clear the STOP flag").set_defaults(fn=cmd_resume)
    sub.add_parser("status", help="containers, locks, agents").set_defaults(fn=cmd_status)
    d = sub.add_parser("dashboard", help="live web dashboard"); d.add_argument("--port", type=int); d.set_defaults(fn=cmd_dashboard)
    dm = sub.add_parser("demo", help="topology -> baseline -> experiments -> analysis -> narrative")
    dm.add_argument("--experiments", type=int, default=3); dm.add_argument("--baseline", type=int, default=45); dm.add_argument("--no-llm", action="store_true"); dm.add_argument("--allow-protected", action="store_true")
    dm.set_defaults(fn=cmd_demo)

    a = p.parse_args(argv)
    s = load_settings(a.config)
    if getattr(a, "port", None) is None and a.cmd == "dashboard":
        a.port = s.dashboard_port
    store = ReportStore(s.reports)
    try:
        return a.fn(s, store, a)
    except KeyboardInterrupt:
        say("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
