"""A live test campaign: every scenario, end to end, on the running demo, through the running server's HTTP API.

For each scenario: wait until the system is quiet, inject the fault (practice API), wait for the agents to raise
and plan an incident, approve it as a named test operator (the same call the Control Room makes), wait for
execution, verification and the report, revert the fault, score it, and let the demo settle. Nothing is simulated:
the telemetry, the agents' tool calls, the Action API calls and the reports are all real. Results go to
practice_dir/campaign-<timestamp>.json and an evaluation note in Markdown.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

import requests

TERMINAL = ("resolved", "dry_run_complete", "escalated", "rejected", "failed")

# One or more scenarios per fault type in the brief's list of ten.
BRIEF_FAULTS = [
    ("1 Payment fails when charging", ["payment-charge-fails"]),
    ("2 Cart fails intermittently", ["cart-intermittent"]),
    ("3 Catalog fails for one product", ["catalog-one-product"]),
    ("4 Recommendation memory leak", ["recommendation-cache-leak"]),
    ("5 Ad high CPU / heavy GC", ["ad-high-cpu", "ad-gc-pressure"]),
    ("6 Homepage traffic flood", ["homepage-flood"]),
    ("7 Container stopped / restarting", ["cart-stopped", "shipping-crash-loop"]),
    ("8 Cache or database unavailable", ["valkey-down", "catalog-db-down"]),
    ("9 Network latency / packet loss", ["catalog-latency", "quote-packet-loss"]),
    ("10 CPU / memory starvation", ["checkout-cpu-starved", "currency-memory-starved"]),
]


class Campaign:
    def __init__(self, base: str, operator: str, out_dir: str, log: Callable[[str], None] = print,
                 detect_timeout: float = 420, finish_timeout: float = 600, settle_timeout: float = 420):
        self.base, self.operator, self.out = base.rstrip("/"), operator, Path(out_dir)
        self.log, self.detect_timeout, self.finish_timeout = log, detect_timeout, finish_timeout
        self.settle_timeout = settle_timeout

    def _get(self, path: str):
        return requests.get(self.base + path, timeout=30).json()

    def _post(self, path: str, body: dict):
        r = requests.post(self.base + path, json=body, timeout=120)
        return r.status_code, r.json()

    def _open(self) -> list[dict]:
        return [i for i in self._get("/api/incidents") if i["phase"] not in TERMINAL]

    def _quiet(self) -> bool:
        if self._open():
            return False
        snap = self._get("/api/services")
        for name, s in (snap.get("services") or {}).items():
            c, n = s.get("container") or {}, s.get("now") or {}
            if c and not c.get("running"):
                return False
            if n and n.get("rps", 0) >= 0.01 and n.get("error_ratio", 0) >= 0.05:
                return False
        return True

    def settle(self) -> bool:
        end = time.time() + self.settle_timeout
        while time.time() < end:
            if self._quiet():
                return True
            time.sleep(10)
        return False

    def run_one(self, scenario_id: str, live: bool) -> dict:
        res: dict = {"scenario": scenario_id, "started": time.time(), "notes": []}
        if not self.settle():
            res["notes"].append("system was not quiet before injection (open incident or failing service)")
        known = {i["id"] for i in self._get("/api/incidents")}
        code, run = self._post("/api/practice/run", {"scenario_id": scenario_id, "duration_s": 1500})
        if code != 200:
            res["error"] = run.get("error")
            return res
        res["run_id"] = run["id"]
        self.log(f"[{scenario_id}] injected ({run['id']})")
        # The fault stays injected until every incident it causes has closed: approve each plan as it appears
        # (found live: reverting after the first incident let the fix "succeed" on an already-healed system).
        approved: set[str] = set()
        first_seen: Optional[float] = None
        end = time.time() + self.detect_timeout + self.finish_timeout
        while time.time() < end:
            new = [i for i in self._get("/api/incidents") if i["id"] not in known]
            if new and first_seen is None:
                first_seen = time.time()
                res["detected_after_s"] = round(first_seen - res["started"], 1)
            for i in new:
                if i["phase"] == "awaiting_approval" and i["id"] not in approved:
                    inc = self._get(f"/api/incidents/{i['id']}")
                    self.log(f"[{scenario_id}] {i['id']} plan: "
                             f"{[a['action'] + ' ' + json.dumps(a['parameters']) for a in inc['plan']['actions']]}")
                    code, body = self._post(f"/api/incidents/{i['id']}/decision",
                                            {"decision": "approved", "approver": self.operator,
                                             "change_hash": inc["plan"]["change_hash"]})
                    approved.add(i["id"])
                    if code != 200:
                        res["notes"].append(f"approval of {i['id']} refused: {body}")
            if new and all(i["phase"] in TERMINAL for i in new):
                break
            if first_seen is None and time.time() - res["started"] > self.detect_timeout:
                break
            time.sleep(5)
        new = [i for i in self._get("/api/incidents") if i["id"] not in known]
        res["incidents"] = [i["id"] for i in new]
        if not new:
            res["notes"].append("nothing detected within the detection window")
        else:
            inc_id = new[-1]["id"]  # the oldest new incident (the list is newest first)
            res["incident"] = inc_id
            if any(i["phase"] not in TERMINAL for i in new):
                res["notes"].append("an incident was still open when the time limit ended the run")
            inc = self._get(f"/api/incidents/{inc_id}")
            res["phase"] = inc["phase"]
            d = inc.get("diagnosis") or {}
            res["diagnosis"] = [{"component": r["component"], "category": r["category"], "cause": r["cause"],
                                 "confidence": r["confidence"]} for r in d.get("root_causes", [])]
            res["diagnosed_by"] = d.get("produced_by")
            res["audits"] = [a["passed"] for a in inc.get("audits", [])]
            res["actions"] = [{"action": r["action"], "parameters": r["parameters"], "ok": r["ok"],
                               "dry_run": r["dry_run"], "message": r["message"]} for r in inc["execution"]["results"]]
            res["verification"] = {"checked": inc["verification"]["checked"],
                                   "recovered": inc["verification"]["recovered"], "notes": inc["verification"]["notes"]}
            res["escalation_reason"] = inc.get("escalation_reason")
            res["evidence_items"] = len(self._get(f"/api/incidents/{inc_id}/evidence"))
            rep = self._get(f"/api/incidents/{inc_id}/report")
            res["report_status"] = (rep.get("json") or {}).get("status")
        code, done = self._post(f"/api/practice/runs/{res['run_id']}/stop", {})
        res["score"] = (done.get("score") or {}) if code == 200 else {}
        res["truth"] = done.get("truth")
        res["revert_notes"] = done.get("notes")
        res["elapsed_s"] = round(time.time() - res["started"], 1)
        self.log(f"[{scenario_id}] -> {res.get('phase')} score {res['score'].get('points')}/10 "
                 f"diagnosis {[x['component'] + '/' + x['category'] for x in res.get('diagnosis', [])]}")
        return res

    def run(self, scenarios: list[str], live: bool) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.out / f"campaign-{stamp}.json"
        code, st = self._post("/api/mode", {"dry_run": not live, "actor": self.operator})
        results: list[dict] = []
        for sid in scenarios:
            results.append(self.run_one(sid, live))
            path.write_text(json.dumps({"live": live, "operator": self.operator, "results": results}, indent=2))
        (self.out / f"evaluation-{stamp}.md").write_text(evaluation_note(results, live))
        return path


def evaluation_note(results: list[dict], live: bool) -> str:
    by_id = {r["scenario"]: r for r in results}
    lines = ["# Evaluation note", "", f"Live campaign on the running OpenTelemetry Demo ({'live' if live else 'dry-run'} "
             "mode). Every result below is from real telemetry, real agent tool calls and real Action API calls.", "",
             "| Brief fault | Scenario | Detected | Diagnosis | Action | Outcome | Score |", "|---|---|---|---|---|---|---|"]
    for label, sids in BRIEF_FAULTS:
        for sid in sids:
            r = by_id.get(sid)
            if r is None:
                continue
            diag = ", ".join(f"{d['component']} ({d['category']}, {d['confidence']:.2f})" for d in r.get("diagnosis", [])) or "-"
            acts = ", ".join(f"{a['action']} {json.dumps(a['parameters'])}" for a in r.get("actions", [])) or "-"
            det = f"{r['detected_after_s']}s" if "detected_after_s" in r else "no"
            lines.append(f"| {label} | {sid} | {det} | {diag} | {acts} | {r.get('phase', '-')} | "
                         f"{(r.get('score') or {}).get('points', '-')}/10 |")
    for r in results:
        if r["scenario"] not in {s for _, ss in BRIEF_FAULTS for s in ss}:
            lines.append(f"| combination | {r['scenario']} | {'yes' if 'incident' in r else 'no'} | "
                         f"{', '.join(d['component'] for d in r.get('diagnosis', [])) or '-'} | "
                         f"{', '.join(a['action'] for a in r.get('actions', [])) or '-'} | {r.get('phase', '-')} | "
                         f"{(r.get('score') or {}).get('points', '-')}/10 |")
    lines += ["", "## Why each result came out the way it did", ""]
    for r in results:
        sc = (r.get("score") or {}).get("detail", {})
        why = "; ".join(r.get("notes", []) + ([r["escalation_reason"]] if r.get("escalation_reason") else []))
        lines.append(f"- **{r['scenario']}**: truth {((r.get('truth') or {}).get('components'))}; named "
                     f"{sc.get('components_named')}; audit {r.get('audits')}; verification "
                     f"{(r.get('verification') or {}).get('notes', '-')}" + (f"; {why}" if why else ""))
    return "\n".join(lines) + "\n"


def default_order() -> list[str]:
    return [s for _, ss in BRIEF_FAULTS for s in ss] + ["combo-valkey-and-payment"]


def main_campaign(base: str, operator: str, out_dir: str, scenarios: Optional[list[str]], live: bool) -> Path:
    return Campaign(base, operator, out_dir).run(scenarios or default_order(), live)
