import type { ReactNode } from "react";
import { ArrowRight, CheckCircle, FileText, Info, XCircle } from "@phosphor-icons/react";
import type { OnboardingComparison, OnboardingReport } from "../../lib/types";
import { fmtPct } from "../../lib/format";
import { Instrument } from "../../components/Station";
import { AiMark, cx } from "../../components/ui";
import { prose } from "./load-util";

function Pair({ pair }: { pair: string }) {
  const [src, dst] = pair.split(/\s*->\s*/);
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 font-mono text-[13px] text-fg">
      {src}
      <ArrowRight size={12} aria-hidden="true" className="text-fg-2" />
      <span className="sr-only">depends on</span>
      {dst}
    </span>
  );
}

function Sub({ children, ink = "text-fg" }: { children: ReactNode; ink?: string }) {
  return <h4 className={cx("label-condensed text-[13px]", ink)}>{children}</h4>;
}

function Count({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[12px] text-fg-2">{label}</span>
      <span className="font-mono text-[26px] leading-none tabular text-fg">{value}</span>
      {note && <span className="text-[12px] leading-snug text-fg-2">{note}</span>}
    </div>
  );
}

/** Split "Gateway forwards ... (architecture-services-gateway.md)" into the claim and the document it cites. */
function splitCite(evidence: string): { claim: string; cite: string | null } {
  const m = /\(([^()]+\.(?:md|adoc|rst|txt|ya?ml|conf))\)\s*$/i.exec(evidence);
  return m ? { claim: evidence.slice(0, m.index).trim(), cite: m[1] } : { claim: evidence, cite: null };
}

/** What the agent wrote: the knowledge file, what code kept and what it refused. */
export function KnowledgeResult({ report, outputRel, recorded }: { report: OnboardingReport; outputRel: string; recorded: boolean }) {
  const k = report.knowledge;
  const llm = report.generated_by === "llm";
  const roles = Object.entries(k.roles ?? {});
  return (
    <Instrument title={recorded ? "What the agent wrote, recorded" : "What the agent wrote"} meta={outputRel || undefined}>
      <div className="grid grid-cols-2 gap-x-6 gap-y-5 border-b border-line pb-5 sm:grid-cols-4">
        <Count label="Services" value={report.services.length} />
        <Count label="Structural edges" value={report.structural_edges} note="compose and nginx" />
        <Count label="Documented edges kept" value={k.documented_edges.length} note={llm ? "proposed by the model" : "no model ran"} />
        <Count label="Refused by code" value={report.rejected.length} />
      </div>

      <div className="grid gap-x-8 gap-y-6 pt-5 md:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <section className="flex min-w-0 flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Sub>Documented edges the model proposed and code kept</Sub>
          </div>
          {k.documented_edges.length ? (
            <ul className="divide-y divide-line">
              {k.documented_edges.map((e) => {
                const { claim, cite } = splitCite(prose(e.evidence));
                return (
                  <li key={`${e.src}-${e.dst}`} className="flex flex-col gap-1 py-2.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <Pair pair={`${e.src} -> ${e.dst}`} />
                      {llm && <AiMark />}
                    </div>
                    <p className="text-[13px] leading-snug text-fg-2">{claim}</p>
                    {cite && (
                      <p className="flex items-center gap-1.5 font-mono text-[11px] text-fg-2 [overflow-wrap:anywhere]">
                        <FileText size={12} aria-hidden="true" className="shrink-0" />
                        <span className="sr-only">Cites </span>{cite}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-[13px] text-fg-2">
              {llm ? "The model proposed none that survived validation." : "None. Without the model only structural edges are written."}
            </p>
          )}
          <p className="text-[12px] text-fg-2">
            {llm ? <>Written by <span className="font-mono">{report.model}</span>, checked line by line by code.</> : "Structural only: no model call was made."}
          </p>
        </section>

        <section className="flex min-w-0 flex-col gap-5">
          <div className="flex flex-col gap-2">
            <Sub>Refused by code</Sub>
            {report.rejected.length ? (
              <ul className="flex flex-col gap-1.5">
                {report.rejected.map((r, i) => (
                  <li key={i} className="flex items-start gap-2 text-[13px] leading-snug text-fg">
                    <XCircle size={14} weight="fill" className="mt-[2px] shrink-0 text-fail" aria-hidden="true" />
                    <span><span className="sr-only">Refused: </span>{prose(r)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] text-fg-2">Nothing. Every proposal passed validation.</p>
            )}
          </div>

          {report.notes.length > 0 && (
            <div className="flex flex-col gap-2">
              <Sub>Notes</Sub>
              <ul className="flex flex-col gap-1.5">
                {report.notes.map((n, i) => (
                  <li key={i} className="flex items-start gap-2 text-[13px] leading-snug text-fg">
                    <Info size={14} weight="fill" className="mt-[2px] shrink-0 text-fg-2" aria-hidden="true" />
                    <span className="min-w-0 break-words">{prose(n)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <dl className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-[13px]">
            <dt className="text-fg-2">Health port</dt>
            <dd className="font-mono tabular text-fg">{k.health_port ?? "not found"}</dd>
            <dt className="text-fg-2">Cluster alias</dt>
            <dd className="break-all font-mono text-fg">{k.cluster_alias ?? "none"}</dd>
            <dt className="text-fg-2">Role overrides</dt>
            <dd className="text-fg">
              {roles.length ? (
                <ul className="flex flex-col gap-1">
                  {roles.map(([svc, role]) => (
                    <li key={svc} className="font-mono">{svc} <span className="text-fg-2">as</span> {role}</li>
                  ))}
                </ul>
              ) : (
                <span className="text-fg-2">None. Every service keeps the role code inferred from compose.</span>
              )}
            </dd>
            <dt className="text-fg-2">Services</dt>
            <dd className="font-mono text-[12px] leading-relaxed text-fg">{report.services.join(", ")}</dd>
          </dl>
        </section>
      </div>
    </Instrument>
  );
}

function Match({ ok }: { ok: boolean }) {
  return ok ? (
    <span className="inline-flex items-center gap-1.5 text-pass"><CheckCircle size={14} weight="fill" aria-hidden="true" />match</span>
  ) : (
    <span className="inline-flex items-center gap-1.5 text-fail"><XCircle size={14} weight="fill" aria-hidden="true" />differs</span>
  );
}

/** The acceptance check: the generated file against the hand-written one. Ditto is the only repo with ground truth. */
export function Verdict({ c, truthRel }: { c: OnboardingComparison; truthRel: string }) {
  const cov = c.final_graph_coverage;
  const uncovered = new Set(cov.uncovered);
  const missedButStructural = c.edges.missed.filter((m) => !uncovered.has(m));
  const all = cov.covered_by_generated_or_structural === cov.truth_relationships;
  return (
    <Instrument title="Verdict against the hand-written file" meta={truthRel}>
      <div className="grid gap-x-10 gap-y-6 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
        <div className="flex flex-col gap-2">
          <p className="font-mono text-[44px] leading-none tabular text-fg">
            {cov.covered_by_generated_or_structural}
            <span className="text-[22px] text-fg-2"> of {cov.truth_relationships}</span>
          </p>
          <p className="text-[13px] leading-snug text-fg-2">
            relationships in the hand-written file are covered by what the agent generated or what compose already declares.
          </p>
          <p className={cx("inline-flex items-center gap-1.5 text-[13px]", all ? "text-pass" : "text-fail")}>
            {all ? <CheckCircle size={14} weight="fill" aria-hidden="true" /> : <XCircle size={14} weight="fill" aria-hidden="true" />}
            {all ? "all covered" : `${cov.truth_relationships - cov.covered_by_generated_or_structural} not covered`}
          </p>
        </div>
        <dl className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)] gap-x-4 text-[13px]">
          <Row label="Edge precision">
            <span className="font-mono tabular text-fg">{c.edges.precision.toFixed(2)}</span>
            <span className="text-fg-2">{c.edges.matched} of {c.edges.generated} generated edges are in the hand-written file</span>
          </Row>
          <Row label="Edge recall">
            <span className="font-mono tabular text-fg">{c.edges.recall.toFixed(2)}</span>
            <span className="text-fg-2">
              {c.edges.matched} of {c.edges.truth} hand-written edges were generated
              {missedButStructural.length > 0 && `; the other ${missedButStructural.length} are already structural edges`}
            </span>
          </Row>
          <Row label="Roles">
            <span className="font-mono tabular text-fg">{fmtPct(c.roles.accuracy)}</span>
            <span className="text-fg-2">{c.roles.agreed} of {c.roles.compared} services get the same role</span>
          </Row>
          <Row label="Health port">
            <span className="font-mono tabular text-fg">{c.health_port.generated ?? "none"}</span>
            <Match ok={c.health_port.match} />
          </Row>
          <Row label="Cluster alias">
            <span className="break-all font-mono text-fg">{c.cluster_alias.generated ?? "none"}</span>
            <Match ok={c.cluster_alias.match} />
          </Row>
        </dl>
      </div>
      {c.edges.missed.length > 0 && (
        <div className="mt-5 flex flex-col gap-2 border-t border-line pt-4">
          <h4 className="label-condensed text-[13px] text-fg">Not generated as documented edges</h4>
          <ul className="flex flex-wrap gap-x-6 gap-y-1.5">
            {c.edges.missed.map((m) => (
              <li key={m} className="flex items-center gap-2">
                <Pair pair={m} />
                <span className={cx("text-[12px]", uncovered.has(m) ? "text-fail" : "text-fg-2")}>{uncovered.has(m) ? "not covered" : "covered by compose"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Instrument>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="border-b border-line py-2 text-fg-2">{label}</dt>
      <dd className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 border-b border-line py-2">{children}</dd>
    </>
  );
}
