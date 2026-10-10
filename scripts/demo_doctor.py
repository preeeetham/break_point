"""Pre-demo readiness check. Read-only towards the stack: it never starts, stops, heals or arms anything.

    .venv/bin/python scripts/demo_doctor.py [--url http://127.0.0.1:8777] [--no-llm] [--max-age-hours 12]

Prints one PASS/WARN/FAIL line per check and exits 1 if anything FAILed. Every check runs in its own
daemon thread with a hard timeout so a hung Docker socket or LLM endpoint cannot stall the doctor.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from faultline.config import ENV_MAP, ROOT as CFG_ROOT, _dotenv, load_settings  # noqa: E402
from faultline.reporting.store import ReportStore  # noqa: E402

PY = ".venv/bin/python"
CORE_SERVICES = ["mongodb", "policies", "things", "things-search", "connectivity", "gateway", "nginx"]
REQUIRED_ENV = ("FAULTLINE_COMPOSE_PROJECT", "DITTO_URL", "DITTO_USER", "DITTO_PASSWORD")
GUARDIAN_FRESH_S = 15
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
Result = tuple[str, str]


def _age(ts: datetime) -> float:
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def _fmt_age(hours: float) -> str:
    return f"{hours * 60:.0f} min" if hours < 1 else f"{hours:.1f} h"


# ---------------------------------------------------------------------- checks
def check_env(s, a) -> Result:
    path = CFG_ROOT / ".env"
    if not path.is_file():
        return FAIL, f"no .env at {path}; fix: cp .env.example .env and edit it"
    env = _dotenv(path)
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    known = sum(1 for k in env if k in ENV_MAP)
    key = "key set" if s.llm_api_key else ("no key needed" if s.llm_provider == "ollama" else "key MISSING")
    msg = (f"{known} settings loaded: project {s.compose_project}, ditto {s.ditto_url} as {s.ditto_user} "
           f"(password {'set' if s.ditto_password else 'MISSING'}), llm {s.llm_provider}:{s.llm_model or 'default'} ({key})")
    if missing:
        return FAIL, f"{msg}; missing in .env: {', '.join(missing)}"
    if key == "key MISSING" and s.llm_enabled:
        return WARN, msg
    return PASS, msg


def check_docker(s, a) -> Result:
    from faultline.chaos.docker_ctl import DockerController

    try:
        d = DockerController(s.compose_project, s.compose_network)
        containers = d.containers()
    except Exception as exc:
        return FAIL, f"docker unreachable ({type(exc).__name__}: {str(exc)[:80]}); is Docker Desktop running?"
    if not containers:
        return FAIL, f"compose project '{s.compose_project}' has no containers; fix: docker compose ls, or set FAULTLINE_COMPOSE_PROJECT"
    topo = None
    try:
        topo = ReportStore(s.reports).topology() if s.reports.is_dir() else None
    except Exception:
        pass
    expected = topo.relevant_services() if topo else CORE_SERVICES
    bad: list[str] = []
    for svc in expected:
        c = containers.get(svc)
        if c is None:
            bad.append(f"{svc} missing")
            continue
        c.reload()
        st = c.attrs.get("State", {})
        health = (st.get("Health") or {}).get("Status")
        if st.get("Paused"):
            bad.append(f"{svc} paused")
        elif c.status != "running":
            bad.append(f"{svc} {c.status}")
        elif health == "unhealthy":
            bad.append(f"{svc} unhealthy")
    running = sum(1 for c in containers.values() if c.status == "running")
    msg = f"project {s.compose_project}: {running}/{len(containers)} running, {len(expected) - len(bad)}/{len(expected)} core services ok"
    if bad:
        return FAIL, f"{msg}; {', '.join(bad)}; fix: {PY} -m faultline heal"
    return PASS, msg


def check_ditto(s, a) -> Result:
    try:
        h = requests.get(f"{s.ditto_url}/health", timeout=4)
        status = ""
        try:
            status = h.json().get("status", "")
        except ValueError:
            pass
        r = requests.get(f"{s.ditto_url}/api/2/things/{s.probe_namespace}:canary",
                         auth=(s.ditto_user, s.ditto_password), timeout=4)
    except requests.RequestException as exc:
        return FAIL, f"{s.ditto_url} unreachable ({type(exc).__name__}); fix: docker compose ls, then {PY} -m faultline heal"
    ms = r.elapsed.total_seconds() * 1000
    if r.status_code in (401, 403):
        return FAIL, f"API auth as {s.ditto_user} rejected (HTTP {r.status_code}); check DITTO_USER / DITTO_PASSWORD in .env"
    if r.status_code not in (200, 404):
        return FAIL, f"API answered HTTP {r.status_code} as {s.ditto_user} (/health {h.status_code} {status})"
    msg = f"/health {h.status_code} {status or '?'}, API as {s.ditto_user} HTTP {r.status_code} in {ms:.0f} ms"
    if h.status_code != 200 or (status and status != "UP"):
        return WARN, msg
    return PASS, msg


def check_prometheus(s, a) -> Result:
    try:
        r = requests.get(f"{s.prometheus_url}/-/ready", timeout=3)
    except requests.RequestException as exc:
        return WARN, f"{s.prometheus_url} unreachable ({type(exc).__name__}); optional"
    return (PASS if r.ok else WARN), f"{s.prometheus_url} HTTP {r.status_code}"


def check_dashboard(s, a) -> Result:
    url = a.url.rstrip("/")
    try:
        # a huge `since` keeps the event list empty so the snapshot stays small
        r = requests.get(f"{url}/api/state", params={"since": 10**9}, timeout=4)
        st = r.json()
    except requests.RequestException as exc:
        return FAIL, f"{url} unreachable ({type(exc).__name__}); fix: {PY} -m faultline dashboard --port {_port(url)}"
    except ValueError:
        return FAIL, f"{url}/api/state did not return JSON (HTTP {r.status_code})"
    if r.status_code != 200 or "run" not in st:
        return FAIL, f"{url}/api/state HTTP {r.status_code}: {str(st.get('error', ''))[:80]}"
    sess = st.get("session") or {}
    msg = (f"{url} up: phase {(st.get('run') or {}).get('phase', '?')}, {'armed' if sess.get('armed') else 'not armed'}, "
           f"{len(st.get('experiments') or [])} experiments, project {sess.get('project', '?')}")
    if sess.get("project") and sess["project"] != s.compose_project:
        return WARN, f"{msg} (doctor expects {s.compose_project}; restart the dashboard after editing .env)"
    if st.get("stop_requested"):
        return WARN, f"{msg}; dashboard sees STOP"
    return PASS, msg


def _port(url: str) -> str:
    tail = url.rsplit(":", 1)[-1]
    return tail if tail.isdigit() else "8777"


def check_llm(s, a) -> Result:
    if a.no_llm:
        return WARN, "skipped (--no-llm)"
    from faultline.planning.llm import client_from_settings

    c = client_from_settings(s)
    if not c.enabled:
        return WARN, "disabled (FAULTLINE_LLM=false); planner and prose run deterministic"
    c.timeout = a.llm_timeout
    t0 = time.time()
    # reasoning models think before answering; small budgets (5 or even 32 tokens) come back empty on gpt-oss
    reply = c.complete("Reply with OK.", "ping", max_tokens=128)
    dt = time.time() - t0
    if reply:
        return PASS, f"{c.describe()} answered in {dt:.1f}s"
    why = c.last_error or "empty reply"
    return WARN, f"{c.describe()} not usable ({why[:90]}); demo falls back to deterministic planning"


def check_artifacts(s, a) -> Result:
    if not s.reports.is_dir():
        return FAIL, f"reports dir {s.reports} missing"
    store = ReportStore(s.reports)
    out: list[str] = []
    level = PASS
    for name, load, stamp, fix in (
        ("topology", store.topology, lambda m: m.generated_at, f"{PY} -m faultline topology"),
        ("baseline", store.baseline, lambda m: m.captured_at, f"{PY} -m faultline baseline --duration 60"),
    ):
        try:
            m = load()
        except Exception as exc:
            return FAIL, f"{name}.json invalid ({type(exc).__name__}); fix: {fix}"
        if m is None:
            return FAIL, f"{name}.json missing; fix: {fix}"
        ts = stamp(m)
        age = _age(ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))
        out.append(f"{name} {_fmt_age(age)} old")
        if age > a.max_age_hours:
            level = WARN
            out[-1] += f" (> {a.max_age_hours:g} h, refresh: {fix})"
    return level, ", ".join(out)


def check_locks(s, a) -> Result:
    if not s.reports.is_dir():
        return FAIL, f"reports dir {s.reports} missing"
    store = ReportStore(s.reports)
    if store.stop_requested():
        return FAIL, f"STOP flag present ({store.stop_file}); fix: {PY} -m faultline resume"
    lock = store.active()
    if store.lock_file.exists() and lock is None:
        return FAIL, f"unreadable lock {store.lock_file}; fix: {PY} -m faultline heal"
    if lock:
        left = float(lock.get("deadline") or 0) - time.time()
        eid = lock.get("experiment_id", "?")
        if left > 0:
            return WARN, f"experiment {eid} is running right now (deadline in {left:.0f}s)"
        return FAIL, f"stale lock for experiment {eid} ({-left:.0f}s past deadline); fix: {PY} -m faultline heal"
    return PASS, "no STOP flag, no experiment lock"


def check_guardian(s, a) -> Result:
    hb = ReportStore(s.reports).heartbeat("guardian") if s.reports.is_dir() else None
    at = float((hb or {}).get("at") or 0)
    if at > time.time() - GUARDIAN_FRESH_S:
        return PASS, f"heartbeat {time.time() - at:.0f}s ago"
    seen = f"last heartbeat {_fmt_age((time.time() - at) / 3600)} ago" if at else "no heartbeat"
    return WARN, f"not running ({seen}); start it in a second terminal: {PY} -m faultline guardian"


CHECKS: list[tuple[str, Callable, float]] = [
    ("env", check_env, 3),
    ("docker", check_docker, 12),
    ("ditto", check_ditto, 10),
    ("prometheus", check_prometheus, 5),
    ("dashboard", check_dashboard, 6),
    ("llm", check_llm, 12),
    ("artifacts", check_artifacts, 4),
    ("locks", check_locks, 3),
    ("guardian", check_guardian, 3),
]
# a timeout is only a FAIL where the demo cannot go on without the thing
SOFT = {"prometheus", "llm", "guardian"}


def run_checks(s, a) -> list[tuple[str, str, str]]:
    results: dict[str, Result] = {}

    def worker(name: str, fn: Callable) -> None:
        try:
            results[name] = fn(s, a)
        except Exception as exc:
            results[name] = (WARN if name in SOFT else FAIL), f"check crashed: {type(exc).__name__}: {str(exc)[:100]}"

    threads = []
    for name, fn, limit in CHECKS:
        if name == "llm":
            limit = a.llm_timeout + 2
        t = threading.Thread(target=worker, args=(name, fn), daemon=True, name=f"doctor-{name}")
        t.start()
        threads.append((name, t, time.time() + limit))
    out = []
    for name, t, deadline in threads:
        t.join(max(0.0, deadline - time.time()))
        level, msg = results.get(name) or ((WARN if name in SOFT else FAIL), "timed out")
        out.append((name, level, msg))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Faultline pre-demo readiness check (read-only)")
    p.add_argument("--url", default="http://127.0.0.1:8777", help="dashboard base URL")
    p.add_argument("--reports", help="blackboard directory (default: settings reports_dir)")
    p.add_argument("--no-llm", action="store_true", help="skip the live LLM call")
    p.add_argument("--llm-timeout", type=float, default=8.0)
    p.add_argument("--max-age-hours", type=float, default=12.0, help="warn when topology/baseline are older")
    a = p.parse_args(argv)
    s = load_settings()
    if a.reports:
        s.reports_dir = str(Path(a.reports).resolve())

    t0 = time.time()
    tty = sys.stdout.isatty()
    colour = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
    print(f"Faultline demo doctor: project {s.compose_project}, reports {s.reports}")
    rows = run_checks(s, a)
    for name, level, msg in rows:
        tag = f"{colour[level]}{level}\033[0m" if tty else level
        print(f"  {tag}  {name:<10} {msg}")
    n = {lv: sum(1 for _, x, _ in rows if x == lv) for lv in (PASS, WARN, FAIL)}
    verdict = "NOT READY" if n[FAIL] else ("ready, with warnings" if n[WARN] else "ready")
    print(f"{n[PASS]} pass, {n[WARN]} warn, {n[FAIL]} fail in {time.time() - t0:.1f}s: {verdict}")
    return 1 if n[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
