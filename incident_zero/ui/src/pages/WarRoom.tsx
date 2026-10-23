import { useState } from "react";
import { Link } from "react-router";
import { Decision, EventRow, PhaseLabel, Pipeline, PlanActions } from "../components";
import { useLive, useLiveData } from "../live";
import type { Event, Incident, IncidentSummary } from "../types";
import { ago, duration, isOpen, until } from "../util";

export default function WarRoom() {
  const { state } = useLive();
  const list = useLiveData<IncidentSummary[]>("/api/incidents");
  const open = (list.data ?? []).filter((i) => isOpen(i.phase));
  const [pick, setPick] = useState<string | null>(null);
  const awaiting = open.find((i) => i.phase === "awaiting_approval");
  const currentId = (pick && open.some((i) => i.id === pick) ? pick : null) ?? awaiting?.id ?? open[0]?.id ?? null;

  if (list.data && !currentId) return <Quiet last={list.data[0]} model={state?.llm ?? null} />;
  return (
    <div className="stack">
      {open.length > 1 && (
        <div className="row small">
          <span className="faint">Open incidents</span>
          {open.map((i) => (
            <button key={i.id} className={`btn small ${i.id === currentId ? "" : "quiet"}`} onClick={() => setPick(i.id)}>
              {i.id}{i.phase === "awaiting_approval" ? " · needs approval" : ""}
            </button>
          ))}
        </div>
      )}
      {currentId && <Active id={currentId} />}
    </div>
  );
}

function Quiet({ last, model }: { last?: IncidentSummary; model: string | null }) {
  return (
    <div className="card empty">
      <h2>All quiet</h2>
      <p>The Sentinel is watching error rates, latency, traffic and container health.</p>
      <p className="small faint" style={{ marginTop: 12 }}>
        {model ? `Model: ${model}` : "No model configured: agents use their deterministic playbooks."}
        {last && <> · Last incident <Link className="t-info" to={`/incidents/${last.id}`}>{last.id}</Link> {ago(last.updated_at)}</>}
      </p>
    </div>
  );
}

function Active({ id }: { id: string }) {
  const { data: inc, reload } = useLiveData<Incident>(`/api/incidents/${id}`);
  const { data: events } = useLiveData<Event[]>(`/api/incidents/${id}/events`);
  if (!inc) return null;
  const d = inc.diagnosis;
  const steps = (events ?? []).filter((e) => e.kind !== "phase").slice(-12).reverse();
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="row">
            <span className="mono faint">{inc.id}</span>
            <PhaseLabel phase={inc.phase} />
            <span className="small faint">open for {duration(inc.created_at)}</span>
          </div>
          <h1 style={{ marginTop: 6 }}>{inc.title}</h1>
          <p className="sub">
            {inc.alerts.filter((a) => a.active).length} active alert(s) on {inc.services.join(", ")}
            {inc.rounds > 1 && ` · investigation round ${inc.rounds}`}
          </p>
        </div>
        <Link className="btn" to={`/incidents/${inc.id}/timeline`}>Open incident</Link>
      </div>

      <Pipeline phase={inc.phase} />

      {inc.phase === "awaiting_approval" && inc.plan && (
        <section className="card">
          <div className="card-head">
            <h2>Decision needed</h2>
            <span className="small faint">escalates {until(inc.approval.expires_at)} without a decision</span>
          </div>
          <div className="stack">
            {d && <p>{d.summary}</p>}
            {d && <Causes inc={inc} />}
            <hr className="divider" />
            <div className="section">
              <h3>Proposed plan</h3>
              <PlanActions inc={inc} />
            </div>
            <Decision inc={inc} onDone={reload} />
          </div>
        </section>
      )}

      <div className="grid-2">
        <section className="section">
          <h3>Live activity</h3>
          <div className="list">
            {steps.length ? steps.map((e) => <EventRow key={e.seq} ev={e} />) : <div className="item muted">Waiting for the first step…</div>}
          </div>
        </section>
        <section className="section">
          <h3>Current hypothesis</h3>
          <div className="card tight stack-s">
            {d ? (
              <>
                <p>{d.summary}</p>
                <Causes inc={inc} compact />
                <p className="small faint">by {d.produced_by === "llm" ? "the model" : "rules"} ·
                  audit {inc.audits.length ? (inc.audits[inc.audits.length - 1].passed ? "passed" : "rejected") : "pending"}</p>
              </>
            ) : (
              <p className="muted">No diagnosis yet. The investigator is collecting evidence.</p>
            )}
          </div>
          <h3 style={{ marginTop: 8 }}>Alerts</h3>
          <div className="list">
            {inc.alerts.map((a) => (
              <div key={a.id} className="item small">
                <div className="item-row">
                  <span className={`dot ${a.active ? "warn" : ""}`} />
                  <span className="mono faint">{a.id}</span>
                  <span className="grow">{a.signal}</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

export function Causes({ inc, compact = false }: { inc: Incident; compact?: boolean }) {
  const d = inc.diagnosis;
  if (!d) return null;
  return (
    <div className="stack-s">
      {d.root_causes.map((rc) => (
        <div key={rc.component} className="stack-s">
          <div className="spread">
            <div className="row">
              <strong>{rc.component}</strong>
              <span className="tag">{rc.category.replace("_", " ")}</span>
            </div>
            <div className="row small muted">
              <div className="conf"><i style={{ width: `${Math.round(rc.confidence * 100)}%` }} /></div>
              {Math.round(rc.confidence * 100)}%
            </div>
          </div>
          {!compact && <p className="muted">{rc.cause}</p>}
          {(compact ? rc.claims.slice(0, 2) : rc.claims).map((c, i) => (
            <div key={i} className="row small" style={{ alignItems: "baseline", flexWrap: "nowrap" }}>
              <Link className="eid" to={`/incidents/${inc.id}/evidence#${c.evidence_id}`}>{c.evidence_id}</Link>
              <span className="quote" style={{ flex: 1 }}>{c.quote}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
