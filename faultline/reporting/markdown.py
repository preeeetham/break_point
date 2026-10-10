from __future__ import annotations

from ..schemas import ExperimentReport, WindowStats


def _w(s: WindowStats) -> str:
    if not s.sample_count:
        return "- | - | -"
    return f"{s.sample_count} | {100 * (s.success_rate or 0):.1f}% | {s.p95_ms}"


def experiment_markdown(r: ExperimentReport) -> str:
    out = [f"# Experiment {r.experiment_id} — {r.failure_type} on {r.target['service']}", "",
           f"Status **{r.status}** · planned by {r.planned_by} · {r.started_at.isoformat(timespec='seconds')} → {r.ended_at.isoformat(timespec='seconds')}", "",
           f"**Why:** {r.rationale}", "", f"**Hypothesis:** {r.hypothesis.statement}", "",
           f"**Result:** {'held' if r.hypothesis_result.held else 'violated' if r.hypothesis_result.held is False else 'n/a'} — {r.hypothesis_result.notes}", "",
           "| Window | Requests | Success | p95 ms |", "|---|---|---|---|",
           f"| before | {_w(r.baseline)} |", f"| during | {_w(r.during)} |", f"| after | {_w(r.after)} |", ""]
    rec = r.recovery
    out += ["## Recovery", "", f"- recovered: {rec.recovered}, time to recover {rec.time_to_recover_s}s, outage seen by clients {rec.outage_duration_s}s",
            f"- Docker auto-restart: {rec.auto_restarted_by_docker}", f"- remediation actions: {rec.remediation_actions or 'none'}",
            f"- data integrity: {rec.data_integrity_ok} ({rec.data_integrity_detail})", ""]
    if r.health:
        out += ["## Health signals during the fault", "", "| Service | checks | /alive OK | /ready OK | docker unhealthy | container down |", "|---|---|---|---|---|---|"]
        for h in r.health:
            out.append(f"| {h.service} | {h.ticks} | {h.alive_ok_fraction} | {h.ready_ok_fraction} | {h.docker_unhealthy_seen} | {h.container_down_seen} |")
        out.append("")
    out += ["## Findings", ""] + ([f"- **{f.severity}** `{f.code}` — {f.title}. {f.evidence}" for f in r.findings] or ["- none"]) + [""]
    out += ["## Safety", "", f"- aborted early: {r.safety.aborted_early} ({r.safety.abort_reason}), watchdog fired: {r.safety.watchdog_fired}, operator stop: {r.safety.operator_stop}", ""]
    out += ["## Timeline", ""] + [f"- +{e.t_plus_s:6.1f}s  {e.event}  {e.detail}" for e in r.timeline] + [""]
    out += ["## Reproduce by hand", "", "```sh"] + r.reproduce + ["```", ""]
    if r.error:
        out += [f"Error: {r.error}", ""]
    return "\n".join(out)
