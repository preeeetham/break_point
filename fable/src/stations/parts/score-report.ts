// F4: the findings report as Markdown, built in the browser from /api/state.
// Same section order as the Day-2 docs/FINDINGS.md, minus its hand-written analysis. Every number is the
// API's own value at its own precision; every string passes through tidy() so no em or en dash survives.
import type { ExperimentSummary, Finding, Recommendation, ScoreRow, State } from "../../lib/types";
import { abortDetail, didRun, isAutoStop, isModelExtra, isOperatorStop, plural, rankFindings, SEV_RANK, stopAt, tPlus } from "./score-rollup";
import { byModel, noteList, stripLlmTag, tidy } from "./score-text";

const RANGE = /(\d)\s*[\u2013\u2014]\s*(\d)/g;
const ANY_DASH = /[\u2013\u2014]/g;

/** Dash-free text: numeric ranges read "3 to 5", every other dash goes through tidy(). */
function t(text: string | null | undefined): string {
  return tidy((text ?? "").replace(RANGE, "$1 to $2"));
}

/** Text safe inside a Markdown table cell. */
function cell(text: string | null | undefined): string {
  return t(text).replace(/\r?\n+/g, " ").replace(/\|/g, "\\|");
}

function code(text: string | null | undefined): string {
  const v = t(text);
  return v ? `\`${v}\`` : "n/a";
}

function n(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? "n/a" : String(v);
}

/** A 0..1 rate as a percentage, without rounding beyond float noise. */
function pct(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? "n/a" : `${Number((v * 100).toFixed(6))}%`;
}

function ms(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? "n/a" : `${v} ms`;
}

function secs(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? "n/a" : `${v} s`;
}

/** Scorecard scores arrive with one decimal; keep it (99.0 stays 99.0). */
function score(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? "n/a" : v.toFixed(1);
}

/** UTC, whole seconds, truncated: 2026-10-11 04:23:04. */
function utc(iso: string): string {
  const d = new Date(iso);
  const s = Number.isNaN(d.getTime()) ? iso : d.toISOString();
  return s.slice(0, 19).replace("T", " ");
}

const startMs = (e: ExperimentSummary) => Date.parse(e.started_at) || 0;
const chronological = (list: ExperimentSummary[]) => [...list].sort((a, b) => startMs(a) - startMs(b));

function params(p: Record<string, number | boolean> | null | undefined): string {
  const entries = Object.entries(p ?? {});
  return entries.length ? entries.map(([k, v]) => `${k}=${v}`).join(", ") : "none";
}

function untested(r: ScoreRow): boolean {
  return r.score == null || !/^[A-F]$/.test(r.grade);
}

function riskText(e: ExperimentSummary): string {
  return e.risk ? `${n(e.risk.severity)} ${t(e.risk.level)}` : "not assessed";
}

function safetyText(e: ExperimentSummary): string {
  if (!didRun(e)) return "skipped by pre-flight, nothing injected";
  if (!e.aborted_early && !e.watchdog_fired) return "ran full duration";
  if (isOperatorStop(e)) return "aborted: operator stop";
  if (byWatchdog(e)) return `stopped by the watchdog at the hard deadline${e.abort_reason ? ` (${t(e.abort_reason)})` : ""}`;
  return `aborted: ${t(e.abort_reason) || "no reason recorded"}`;
}

/** The watchdog cut the fault short, not the safety monitor: it fired and the monitor logged no stop of its own. */
function byWatchdog(e: ExperimentSummary): boolean {
  return e.watchdog_fired && tPlus(e, "safety_abort") == null;
}

function hypothesisResult(e: ExperimentSummary): string {
  if (e.hypothesis_held === true) return "held";
  if (e.hypothesis_held === false) return "violated";
  return "not judged";
}

function cap(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function actions(list: unknown[] | null | undefined): string {
  return list?.length ? list.map((x) => t(typeof x === "string" ? x : JSON.stringify(x))).join("; ") : "none";
}

function title(r: Recommendation): string {
  return t(stripLlmTag(r.title).text);
}

// ---------------------------------------------------------------- sections

function header(s: State, exps: ExperimentSummary[]): string[] {
  const project = s.target?.project ?? s.session?.project ?? "unknown";
  const starts = exps.map((e) => e.started_at).filter(Boolean).sort((a, b) => Date.parse(a) - Date.parse(b));
  const ends = exps.map((e) => e.ended_at).filter((x): x is string => !!x).sort((a, b) => Date.parse(a) - Date.parse(b));
  const window = starts.length
    ? `run window ${utc(starts[0])} to ${ends.length ? utc(ends[ends.length - 1]) : "still running"} UTC`
    : "no experiments recorded";
  return [
    `# Faultline findings report: ${t(project)}`,
    "",
    `Project \`${t(project)}\`, ${window}.`,
    "",
    "> Generated in the browser from /api/state. Hand-written analysis in docs/FINDINGS.md is not included.",
    "",
  ];
}

function summary(s: State, exps: ExperimentSummary[]): string[] {
  const sc = s.scorecard;
  const ran = exps.filter(didRun);
  const skipped = exps.length - ran.length;
  const types = [...new Set(ran.map((e) => e.failure_type))].sort();
  const rated = exps.filter((e) => e.risk && (e.risk.level === "high" || e.risk.level === "critical")).length;
  const worst = [...exps].filter((e) => e.risk).sort((a, b) => b.risk!.severity - a.risk!.severity || startMs(a) - startMs(b))[0];
  const stops = exps.filter(isAutoStop);
  const stoppedBy = stops.some(byWatchdog) ? "a safety threshold or the watchdog" : "a safety threshold";
  const out = ["## Summary", ""];
  out.push(`- **Overall resilience score: ${score(sc?.overall_score)} / 100** across ${plural(ran.length, "experiment")}.`);
  if (skipped) out.push(`- ${plural(skipped, "experiment")} skipped by pre-flight, nothing injected.`);
  if (sc?.summary) out.push(`- Scorecard: ${t(sc.summary)}`);
  out.push(`- **${plural(types.length, "distinct failure type")}**: ${types.map((x) => code(x)).join(", ") || "none"}.`);
  out.push(
    `- **${rated} rated high or critical** by the Risk agent` +
      (worst ? `; the worst is ${code(worst.failure_type)} on ${code(worst.target_service)} at **${n(worst.risk!.severity)}/10 (${t(worst.risk!.level)})**.` : "."),
  );
  out.push(`- **${plural(stops.length, "experiment")} stopped automatically** by ${stoppedBy} and rolled back.`);
  out.push("- Every recommendation is advisory only; Faultline never changes the system under test.");
  out.push("");
  return out;
}

function ranked(exps: ExperimentSummary[]): string[] {
  const rows = rankFindings(exps);
  const out = ["## Ranked weaknesses", "", "Every finding above info, worst first: finding severity, then the experiment's risk, then run order.", ""];
  if (!rows.length) return [...out, "No finding above info.", ""];
  out.push("| # | Severity | Finding | Title | Experiment | Target | Top recommended fix |", "|---|---|---|---|---|---|---|");
  for (const r of rows) {
    const fix = r.fix ? `${cell(title(r.fix))}${r.fixByModel ? " (model suggestion, unverified)" : ""}` : "none";
    out.push(
      `| ${r.rank} | ${t(r.finding.severity)} | ${code(r.finding.code)} | ${cell(r.finding.title)} | \`${t(r.exp.experiment_id)}\` (${t(r.exp.failure_type)}) | ${code(r.exp.target_service)} | ${fix} |`,
    );
  }
  out.push("");
  return out;
}

function scorecard(s: State): string[] {
  const out = ["## Resilience scorecard", ""];
  const rows = s.scorecard?.ranking ?? [];
  if (!rows.length) return [...out, "No scorecard yet.", ""];
  out.push("| Service | Score | Grade | Experiments | Worst success during | Slowest recovery | Findings |", "|---|---|---|---|---|---|---|");
  for (const r of rows) {
    const u = untested(r);
    out.push(
      `| ${code(r.service)} | ${u ? "untested" : score(r.score)} | ${u ? "untested" : t(r.grade)} | ${r.experiments} | ${u ? "n/a" : pct(r.worst_success_rate)} | ${u ? "n/a" : secs(r.slowest_recovery_s)} | ${r.findings} |`,
    );
  }
  out.push("");
  return out;
}

function experimentsRun(s: State, exps: ExperimentSummary[]): string[] {
  const out = ["## Experiments run", ""];
  const b = s.baseline;
  if (b) {
    const th = b.thresholds;
    out.push(
      `Baseline before any injection: ${secs(b.duration_s)}, ${b.api.sample_count} requests, success ${pct(b.api.success_rate)}, p50 ${ms(b.api.p50_ms)}, p95 ${ms(b.api.p95_ms)}. ` +
        `Abort thresholds: error rate above ${pct(th.abort_error_rate_above)} over the trailing ${th.trailing_samples} requests, or p95 above ${ms(th.abort_p95_ms_above)}.`,
      "",
    );
  } else {
    out.push("No baseline recorded.", "");
  }
  if (!exps.length) return [...out, "No experiments yet.", ""];
  out.push(
    "| Experiment | Target | Failure | Parameters | Planned by | Success baseline / during / after | p95 during | Time to recover | Safety | Risk |",
    "|---|---|---|---|---|---|---|---|---|---|",
  );
  for (const e of chronological(exps)) {
    out.push(
      `| \`${t(e.experiment_id)}\` | ${code(e.target_service)} | ${code(e.failure_type)} | ${cell(params(e.parameters))} | ${t(e.planned_by)} | ${pct(e.baseline_success_rate)} / ${pct(e.during_success_rate)} / ${pct(e.after_success_rate)} | ${ms(e.during_p95_ms)} | ${secs(e.time_to_recover_s)} | ${cell(safetyText(e))} | ${riskText(e)} |`,
    );
  }
  out.push("");
  return out;
}

function fixLines(r: Recommendation, i: number): string[] {
  const extra = isModelExtra(r);
  const rationale = t(stripLlmTag(r.rationale).text);
  const out = [
    `${i + 1}. **${title(r)}** (${t(r.category)}, effort ${t(r.effort)}, confidence ${n(r.confidence)})${extra ? " (model suggestion, unverified)" : ""}.${rationale ? ` ${rationale}` : ""}`,
  ];
  for (const step of r.steps ?? []) out.push(`   - ${t(step)}`);
  for (const v of r.verify ?? []) out.push(`   - *Verify*: ${t(v)}`);
  return out;
}

function perExperiment(exps: ExperimentSummary[]): string[] {
  const out = ["## Findings and fixes, per experiment (worst risk first)", ""];
  if (!exps.length) return [...out, "No experiments yet.", ""];
  const order = [...exps].sort((a, b) => (b.risk?.severity ?? -1) - (a.risk?.severity ?? -1) || startMs(a) - startMs(b));
  for (const e of order) {
    const model = e.planned_by === "llm" ? " (model)" : "";
    const notes = noteList(e.hypothesis_notes);
    const head = e.risk ? `${cap(t(e.risk.level))} ${n(e.risk.severity)}/10` : "Not assessed";
    out.push(`### ${head}: ${code(e.failure_type)} on ${code(e.target_service)} (${t(e.experiment_id)})`, "");
    out.push(`- **Planned by**: ${t(e.planned_by)}`);
    out.push(`- **Why this experiment**${model}: ${t(e.rationale) || "no rationale recorded"}`);
    out.push(`- **Hypothesis**${model}: ${t(e.hypothesis?.statement) || "none recorded"}`);
    out.push(`- **Hypothesis result**: ${hypothesisResult(e)}${notes.length ? `; ${notes.join("; ")}` : ""}`);
    out.push(
      `- **Errors and slowdown**: success ${pct(e.baseline_success_rate)} before (p95 ${ms(e.baseline_p95_ms)}), ${pct(e.during_success_rate)} during (p95 ${ms(e.during_p95_ms)}), ${pct(e.after_success_rate)} after.`,
    );
    out.push(
      `- **Recovery**: ${e.recovered ? "recovered" : e.recovered === false ? "did not recover" : "recovery not judged"}, time to recover ${secs(e.time_to_recover_s)}; remediation actions: ${actions(e.remediation_actions)}.`,
    );
    out.push(`- **Safety**: ${safetyText(e)}.`);
    out.push(`- **Observed blast radius**: ${(e.affected_services_observed ?? []).map((x) => code(x)).join(", ") || "none"}`);
    out.push("");

    const findings = [...(e.findings ?? [])].sort((a: Finding, b: Finding) => (SEV_RANK[b.severity] ?? 0) - (SEV_RANK[a.severity] ?? 0));
    if (findings.length) {
      out.push("| Severity | Finding | Title | Evidence |", "|---|---|---|---|");
      for (const f of findings) out.push(`| ${t(f.severity)} | ${code(f.code)} | ${cell(f.title)} | ${cell(f.evidence)} |`);
      out.push("");
    }

    if (e.risk) {
      const label = byModel(e.risk.generated_by) ? "model prose" : "templated";
      out.push(`**Risk assessment** (${label}): ${t(e.risk.summary)}`, "");
      out.push(`**Production implication** (${label}): ${t(e.risk.production_implication)}`, "");
      const factors = Object.entries(e.risk.factors ?? {}).map(([k, v]) => `${t(k)} ${n(v)}`).join(", ");
      if (factors) out.push(`Score factors: ${factors}`, "");
    }

    const recs = e.fix?.recommendations ?? [];
    if (recs.length) {
      out.push(`**Recommended fixes** (advisory only, generated by ${t(e.fix?.generated_by) || "n/a"}):`, "");
      recs.forEach((r, i) => out.push(...fixLines(r, i)));
      out.push("");
    }
  }
  return out;
}

const STOP_EVENTS = ["injected", "safety_abort", "watchdog_fired", "rollback_done", "recovered"];

function autoStops(exps: ExperimentSummary[]): string[] {
  const out = ["## Automatic stops", ""];
  const stops = chronological(exps.filter(isAutoStop));
  if (!stops.length) return [...out, "No experiment was stopped automatically.", ""];
  for (const e of stops) {
    const d = abortDetail(e);
    const nums = Object.entries(d).map(([k, v]) => `${t(k)} ${n(v)}`).join(", ");
    out.push(`### \`${t(e.experiment_id)}\`: ${code(e.failure_type)} on ${code(e.target_service)}`, "");
    if (byWatchdog(e)) {
      const at = stopAt(e);
      const reason = e.abort_reason ? `, reason ${code(e.abort_reason)}` : "";
      out.push(`- **Stopped by**: the watchdog at the hard deadline${at != null ? `, t+${n(at)} s` : ""}${reason}.`);
    } else {
      out.push(`- **Stopped by**: the safety monitor, reason ${code(e.abort_reason)}${nums ? ` (${nums})` : ""}.`);
    }
    for (const ev of STOP_EVENTS) {
      const entry = e.timeline?.find((x) => x.event === ev);
      if (entry) out.push(`- t+${n(entry.t_plus_s)} s \`${ev}\`${entry.detail ? `: ${t(entry.detail)}` : ""}`);
    }
    out.push(`- **Time to recover**: ${secs(e.time_to_recover_s)}`);
    out.push(`- **Remediation**: ${actions(e.remediation_actions)}`);
    const integrity = e.data_integrity_ok === true ? "ok" : e.data_integrity_ok === false ? "failed" : "not checked";
    out.push(`- **Data integrity**: ${integrity}${e.data_integrity_detail ? ` (${t(e.data_integrity_detail)})` : ""}`);
    out.push("");
  }
  return out;
}

/** F4: the findings report as Markdown, built from /api/state. */
export function buildFindingsReport(s: State): string {
  const exps = s.experiments ?? [];
  const md = [
    ...header(s, exps),
    ...summary(s, exps),
    ...ranked(exps),
    ...scorecard(s),
    ...experimentsRun(s, exps),
    ...perExperiment(exps),
    ...autoStops(exps),
  ].join("\n");
  // belt and braces: nothing above should leave a dash, but the report must never carry one
  return md.replace(ANY_DASH, "-");
}

/** Save the report as faultline-findings-<project>-<date>.md through a Blob URL that is revoked after use. */
export function downloadFindingsReport(s: State): string {
  const md = buildFindingsReport(s);
  const project = (s.target?.project ?? s.session?.project ?? "faultline").replace(/[^\w.-]+/g, "-");
  const name = `faultline-findings-${project}-${new Date().toISOString().slice(0, 10)}.md`;
  const url = URL.createObjectURL(new Blob([md], { type: "text/markdown;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // revoking right away can cancel the download in some browsers; give it time to start
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return name;
}
