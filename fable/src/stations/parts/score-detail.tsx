// One experiment as a report sheet, in reading order: what was done, what it did, whether the
// guess held, what code found, how bad it is, what to change, how to do it again by hand.
import { forwardRef, type ReactNode } from "react";
import { ArrowSquareOut, CheckCircle, Warning, XCircle } from "@phosphor-icons/react";
import type { ExperimentSummary, Finding } from "../../lib/types";
import { failureLabel, fmtMs, fmtS, relTime } from "../../lib/format";
import { AiMark, cx } from "../../components/ui";
import { CopyButton, HeldMark, Rich, SeverityMark, SheetSection, StatusMark } from "./score-marks";
import { abortExplain, abortWho, byModel, noteList, pctSmart, tidy, when } from "./score-text";
import { usePhases } from "./score-data";
import { RiskPanel } from "./score-risk";
import { Fixes } from "./score-fixes";
import { NarrativeTimeline } from "./score-narrative";

const ISSUE_1865 = "https://github.com/eclipse-ditto/ditto/issues/1865";
const MASKED = "health_check_masked_failure";
const SEV_RANK: Record<Finding["severity"], number> = { critical: 4, high: 3, medium: 2, low: 1, info: 0 };

export const ExperimentDetail = forwardRef<HTMLDivElement, { exp: ExperimentSummary; now: number }>(function ExperimentDetail({ exp, now }, ref) {
  const id = exp.experiment_id;
  const aborted = exp.aborted_early || exp.status === "aborted";
  return (
    <div ref={ref} id="exp-detail" role="tabpanel" aria-labelledby={`exp-tab-${id}`} className="min-w-0 scroll-mt-32 px-4 md:px-6">
      <header className="flex flex-col gap-2 pb-5 pt-4">
        <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-fg-2">
          <span className="font-mono tabular text-fg">{id}</span>
          <StatusMark status={exp.status} />
          <span>
            {when(exp.started_at)}, {relTime(exp.started_at, now)}
          </span>
        </p>
        <h4 className="display text-[clamp(1.5rem,2.4vw,2rem)] text-fg">
          {failureLabel(exp.failure_type)} on {exp.target_service}
        </h4>
      </header>
      <WhatWasDone exp={exp} />
      <Measured exp={exp} />
      {aborted && <Safety exp={exp} />}
      <Hypothesis exp={exp} />
      <Findings findings={exp.findings} eid={id} />
      <SheetSection label="Risk" aside="Risk agent">
        <RiskPanel risk={exp.risk} findings={exp.findings} eid={id} />
      </SheetSection>
      <SheetSection label="Fixes" aside="Fix agent">
        <Fixes fix={exp.fix} />
      </SheetSection>
      {exp.reproduce.length > 0 && <Reproduce lines={exp.reproduce} />}
      <SheetSection label="Play-by-play" aside="The Narrator, templated by code. It quotes the risk summary as stored.">
        <NarrativeTimeline eid={id} riskByModel={byModel(exp.risk?.generated_by)} />
      </SheetSection>
    </div>
  );
});

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-[13px] text-fg-2 sm:pt-0.5">{k}</dt>
      <dd className="min-w-0 text-[14px] text-fg">{children}</dd>
    </>
  );
}

function plannedBy(p: ExperimentSummary["planned_by"]): ReactNode {
  if (p === "llm") return <span className="inline-flex flex-wrap items-center gap-2 text-model"><AiMark /> the model, choosing from the five candidates code ranked highest</span>;
  if (p === "deterministic") return "code: the top-ranked candidate, no model involved";
  return "an operator, by hand";
}

function WhatWasDone({ exp }: { exp: ExperimentSummary }) {
  const params = Object.entries(exp.parameters ?? {});
  const llm = exp.planned_by === "llm";
  return (
    <SheetSection label="What was done">
      <dl className="grid gap-x-6 gap-y-2.5 sm:grid-cols-[7rem_minmax(0,1fr)]">
        <Row k="Target">
          <span className="font-mono">{exp.target_service}</span>
          {exp.criticality_score != null && (
            <span className="ml-3 text-[12px] text-fg-2">
              criticality <span className="font-mono tabular">{exp.criticality_score.toFixed(1)}</span>
            </span>
          )}
        </Row>
        <Row k="Failure">
          {failureLabel(exp.failure_type)} <span className="ml-2 font-mono text-[12px] text-fg-2">{exp.failure_type}</span>
        </Row>
        <Row k="Parameters">
          {params.length ? (
            <span className="flex flex-wrap gap-x-5 gap-y-1 font-mono tabular text-[13px]">
              {params.map(([k, v]) => (
                <span key={k}>
                  <span className="text-fg-2">{k}</span> {String(v)}
                </span>
              ))}
            </span>
          ) : (
            <span className="text-fg-2">none</span>
          )}
        </Row>
        <Row k="Planned by">{plannedBy(exp.planned_by)}</Row>
        <Row k="Rationale">
          <span className={cx("max-w-[68ch] leading-relaxed", llm ? "text-model" : "text-fg")}>
            <Rich text={exp.rationale} />
          </span>
        </Row>
      </dl>
    </SheetSection>
  );
}

function Measured({ exp }: { exp: ExperimentSummary }) {
  const full = usePhases(exp.experiment_id);
  const dropped = exp.during_success_rate != null && exp.baseline_success_rate != null && exp.during_success_rate < exp.baseline_success_rate;
  const noneOk = exp.during_success_rate === 0;
  const statusCounts = full?.during?.status_counts;
  const counts = statusCounts ? Object.entries(statusCounts).map(([k, v]) => `${k} ${v}`).join(", ") : null;
  const cell = "whitespace-nowrap py-2 pr-3 align-baseline sm:pr-4";
  return (
    <SheetSection label="Measured" aside="Synthetic API traffic through nginx">
      <div className="flex flex-col gap-5">
        <table className="w-full max-w-[42rem] border-collapse text-left">
          <caption className="sr-only">API success and p95 latency before, during and after the fault</caption>
          <thead>
            <tr className="text-[12px] text-fg-2">
              <th scope="col" className="w-[4.5rem] pb-1.5 font-normal sm:w-[6.5rem]"><span className="sr-only">Measure</span></th>
              <th scope="col" className="pb-1.5 pr-4 font-normal">Before</th>
              <th scope="col" className="pb-1.5 pr-4 font-normal">During the fault</th>
              <th scope="col" className="pb-1.5 font-normal">After</th>
            </tr>
          </thead>
          <tbody className="font-mono tabular">
            <tr className="border-t border-line">
              <th scope="row" className={cx(cell, "whitespace-normal font-sans text-[13px] font-normal text-fg-2")}>Success</th>
              <td className={cx(cell, "text-[17px] text-fg sm:text-[20px]")}>{pctSmart(exp.baseline_success_rate)}</td>
              <td className={cx(cell, "text-[17px] sm:text-[20px]", dropped ? "text-fail" : "text-fg")}>{pctSmart(exp.during_success_rate)}</td>
              <td className={cx(cell, "text-[17px] text-fg sm:text-[20px]")}>{pctSmart(exp.after_success_rate)}</td>
            </tr>
            <tr className="border-t border-line">
              <th scope="row" className={cx(cell, "whitespace-normal font-sans text-[13px] font-normal text-fg-2")}>p95 latency</th>
              <td className={cx(cell, "text-[15px] text-fg")}>{fmtMs(exp.baseline_p95_ms)}</td>
              <td className={cx(cell, "text-[15px] text-fg")}>
                {exp.during_p95_ms == null && noneOk ? <span className="whitespace-normal font-sans text-[13px] text-fg-2">none, no request succeeded</span> : fmtMs(exp.during_p95_ms)}
              </td>
              <td className={cx(cell, "text-[15px] text-fg")}>{fmtMs(full?.after?.p95_ms)}</td>
            </tr>
            {full && (
              <tr className="border-t border-line">
                <th scope="row" className={cx(cell, "whitespace-normal font-sans text-[13px] font-normal text-fg-2")}>Requests</th>
                <td className={cx(cell, "text-[13px] text-fg-2")}>{full.baseline?.sample_count ?? "n/a"}</td>
                <td className={cx(cell, "text-[13px] text-fg-2")}>
                  {full.during?.sample_count ?? "n/a"}
                  {counts && <span className="block whitespace-normal text-[11px]">{counts}</span>}
                </td>
                <td className={cx(cell, "text-[13px] text-fg-2")}>{full.after?.sample_count ?? "n/a"}</td>
              </tr>
            )}
          </tbody>
        </table>

        <dl className="grid max-w-[46rem] gap-x-6 gap-y-3 sm:grid-cols-2">
          <div>
            <dt className="text-[12px] text-fg-2">Outage, as clients saw it</dt>
            <dd className="font-mono tabular text-[17px] text-fg">{fmtS(exp.outage_duration_s, 1)}</dd>
          </div>
          <div>
            <dt className="text-[12px] text-fg-2">Time to recover after rollback</dt>
            <dd className="font-mono tabular text-[17px] text-fg">{fmtS(exp.time_to_recover_s, 1)}</dd>
          </div>
          <div>
            <dt className="text-[12px] text-fg-2">Recovery</dt>
            <dd className="text-[14px]"><Recovery exp={exp} /></dd>
          </div>
          <div>
            <dt className="text-[12px] text-fg-2">Data integrity</dt>
            <dd className="text-[14px]">
              {exp.data_integrity_ok === true && <span className="inline-flex items-center gap-1.5 text-pass"><CheckCircle size={14} aria-hidden="true" />intact</span>}
              {exp.data_integrity_ok === false && <span className="inline-flex items-center gap-1.5 text-fail"><XCircle size={14} aria-hidden="true" />writes missing</span>}
              {exp.data_integrity_ok == null && <span className="text-fg-2">not checked</span>}
              {exp.data_integrity_detail && <span className="block font-mono text-[12px] text-fg-2">{tidy(exp.data_integrity_detail)}</span>}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-[12px] text-fg-2">Services seen affected</dt>
            <dd className="font-mono text-[13px] text-fg">{exp.affected_services_observed.length ? exp.affected_services_observed.join(", ") : "none"}</dd>
          </div>
        </dl>
      </div>
    </SheetSection>
  );
}

function Recovery({ exp }: { exp: ExperimentSummary }) {
  if (exp.recovered === false) return <span className="inline-flex items-center gap-1.5 text-fail"><XCircle size={14} aria-hidden="true" />did not recover</span>;
  if (exp.remediation_actions.length) {
    return (
      <span className="flex flex-col gap-1">
        <span className="inline-flex items-center gap-1.5 text-fg"><Warning size={14} aria-hidden="true" />recovered after remediation</span>
        <span className="font-mono text-[12px] text-fg-2">{exp.remediation_actions.map((a) => tidy(a)).join(", ")}</span>
      </span>
    );
  }
  if (exp.recovered) {
    return (
      <span className="inline-flex flex-wrap items-center gap-1.5 text-pass">
        <CheckCircle size={14} aria-hidden="true" />
        recovered by itself
        {exp.auto_restarted_by_docker && <span className="text-fg-2">(Docker restarted the container)</span>}
      </span>
    );
  }
  return <span className="text-fg-2">not recorded</span>;
}

function Safety({ exp }: { exp: ExperimentSummary }) {
  const { at, text } = abortExplain(exp);
  const stopped = exp.operator_stop || exp.abort_reason === "operator_stop";
  return (
    <SheetSection label="Safety" aside="Safety monitor">
      <p className="max-w-[68ch] text-[15px] leading-relaxed text-fg">
        {abortWho(exp)}
        {at != null && <> at <span className="font-mono tabular">t+{at.toFixed(1)}&nbsp;s</span></>}
        {stopped ? "." : <>: {text}.</>}
      </p>
      <p className="mt-1.5 max-w-[68ch] text-[13px] leading-snug text-fg-2">
        An aborted run still counts toward the scorecard, but it can never clear a predicted weakness.
      </p>
    </SheetSection>
  );
}

function Hypothesis({ exp }: { exp: ExperimentSummary }) {
  const h = exp.hypothesis;
  const llm = exp.planned_by === "llm";
  const notes = noteList(exp.hypothesis_notes);
  return (
    <SheetSection label="Hypothesis" aside={llm ? <span className="inline-flex items-center gap-1.5"><AiMark /> written by the model</span> : "templated by code"}>
      <div className="flex flex-col gap-3">
        <p className="text-[16px] font-medium"><HeldMark held={exp.hypothesis_held} long /></p>
        <p className={cx("max-w-[68ch] text-[15px] leading-relaxed", llm ? "text-model" : "text-fg")}>{tidy(h?.statement)}</p>
        {h && (
          <dl className="grid max-w-[46rem] gap-x-6 gap-y-2 text-[13px] sm:grid-cols-3">
            <div>
              <dt className="text-[12px] text-fg-2">Error rate, at most</dt>
              <dd className="font-mono tabular text-fg">{pctSmart(h.expected_max_error_rate)}</dd>
            </div>
            <div>
              <dt className="text-[12px] text-fg-2">Recovery, within</dt>
              <dd className="font-mono tabular text-fg">{fmtS(h.expected_recovery_s)}</dd>
            </div>
            <div>
              <dt className="text-[12px] text-fg-2">Affected, at most</dt>
              <dd className="font-mono text-fg">{h.expected_affected.length ? h.expected_affected.join(", ") : "none"}</dd>
            </div>
          </dl>
        )}
        {notes.length > 0 && (
          <div>
            <p className="text-[12px] text-fg-2">Checked by code against the measurements</p>
            <ul className="mt-1 flex flex-col gap-0.5 border-l-2 border-line pl-3 font-mono text-[12.5px] text-fg">
              {notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </SheetSection>
  );
}

// `at` is each finding's index in exp.findings: the Score table jumps to `exp-finding-<eid>-<at>`
type Item = { kind: "one"; f: Finding; at: number } | { kind: "masked"; fs: Finding[]; at: number[] };

// programmatic focus after a click may not match :focus-visible, so the landed finding gets its own ring
const LANDED = "rounded-[2px] focus:outline-2 focus:outline-offset-2 focus:outline-fg focus:outline-solid";

function Findings({ findings, eid }: { findings: Finding[]; eid: string }) {
  const masked = findings.filter((f) => f.code === MASKED);
  const maskedAt = findings.flatMap((f, i) => (f.code === MASKED ? [i] : []));
  const items: Item[] = [];
  let placed = false;
  findings.forEach((f, at) => {
    if (f.code !== MASKED) items.push({ kind: "one", f, at });
    else if (!placed) {
      items.push({ kind: "masked", fs: masked, at: maskedAt });
      placed = true;
    }
  });
  return (
    <SheetSection label="Findings" aside={`${findings.length} raised by code`}>
      {findings.length === 0 ? (
        <p className="text-[14px] text-fg-2">No findings. Nothing crossed a threshold worth reporting.</p>
      ) : (
        <ul className="divide-y divide-line">
          {items.map((it, i) =>
            it.kind === "one" ? (
              <FindingRow key={i} f={it.f} first={i === 0} id={`exp-finding-${eid}-${it.at}`} />
            ) : (
              <MaskedGroup key={i} fs={it.fs} first={i === 0} ids={it.at.map((at) => `exp-finding-${eid}-${at}`)} />
            ),
          )}
        </ul>
      )}
    </SheetSection>
  );
}

const FROW = "grid gap-x-4 gap-y-1 py-3 sm:grid-cols-[5.5rem_minmax(0,1fr)]";

function FindingRow({ f, first, id }: { f: Finding; first: boolean; id: string }) {
  return (
    <li id={id} tabIndex={-1} className={cx(FROW, LANDED, first && "pt-0")}>
      <span className="text-[13px]"><SeverityMark severity={f.severity} /></span>
      <div className="min-w-0">
        <p className="text-[14px] leading-snug text-fg">
          <Rich text={f.title} /> <span className="ml-1 font-mono text-[11px] text-fg-2">{f.code}</span>
        </p>
        {f.evidence && <p className="mt-1 break-words font-mono text-[12px] leading-relaxed text-fg-2">{tidy(f.evidence)}</p>}
      </div>
    </li>
  );
}

function MaskedGroup({ fs, first, ids }: { fs: Finding[]; first: boolean; ids: string[] }) {
  const worst = fs.reduce((a, f) => (SEV_RANK[f.severity] > SEV_RANK[a.severity] ? f : a), fs[0]);
  return (
    <li className={cx(FROW, first && "pt-0")}>
      <span className="text-[13px]"><SeverityMark severity={worst.severity} /></span>
      <div className="min-w-0">
        <p className="text-[15px] font-medium leading-snug text-fg">
          Health checks stayed green while the API failed
          <span className="ml-2 font-mono text-[11px] font-normal text-fg-2">{MASKED}</span>
        </p>
        <p className="mt-1 max-w-[68ch] text-[13px] leading-relaxed text-fg-2">
          {fs.length === 1 ? "One service" : `${fs.length} services`} kept answering /ready with 200 while the synthetic API was failing, so nothing watching
          those health checks would have reacted. It is the failure mode described in Ditto issue #1865: health checks ignore subsystem health.
        </p>
        <a
          href={ISSUE_1865}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-1 inline-flex min-h-11 items-center gap-1.5 text-[13px] text-fg underline decoration-line underline-offset-4 hover:decoration-fg"
        >
          Ditto issue #1865 on GitHub
          <ArrowSquareOut size={13} aria-hidden="true" />
          <span className="sr-only">(opens in a new tab)</span>
        </a>
        <ul className="mt-1 flex flex-col gap-2 border-l-2 border-fail/60 pl-3">
          {fs.map((f, i) => (
            <li key={i} id={ids[i]} tabIndex={-1} className={LANDED}>
              <p className="text-[14px] leading-snug text-fg"><Rich text={f.title} /></p>
              {f.evidence && <p className="break-words font-mono text-[12px] leading-relaxed text-fg-2">{tidy(f.evidence)}</p>}
            </li>
          ))}
        </ul>
      </div>
    </li>
  );
}

function Reproduce({ lines }: { lines: string[] }) {
  const text = lines.join("\n");
  return (
    <SheetSection label="Reproduce it by hand">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-[56ch] text-[13px] leading-snug text-fg-2">
          The same fault without Faultline. Run by hand, nothing watches the error rate or rolls the fault back for you.
        </p>
        <CopyButton text={text} label="Copy commands" />
      </div>
      <pre className="mt-3 whitespace-pre-wrap break-all border-l-2 border-line pl-3 font-mono text-[12.5px] leading-6 text-fg">{text}</pre>
    </SheetSection>
  );
}
