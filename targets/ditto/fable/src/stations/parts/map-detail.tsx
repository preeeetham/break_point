import type { ReactNode } from "react";
import { X } from "@phosphor-icons/react";
import type { EdgeKind, Topology } from "../../lib/types";
import { dependentsOf, edgeKindLabel } from "../../components/TopologyMap";
import { cx } from "../../components/ui";
import { prose } from "./load-util";
import { LAYERS } from "./map-util";

function KindTag({ kind, shown }: { kind: EdgeKind; shown: boolean }) {
  const l = LAYERS.find((x) => x.kind === kind);
  return (
    <span className={cx("inline-flex items-center gap-1.5 text-[12px]", shown ? "text-fg-2" : "text-fg-2/60")}>
      <svg width="16" height="6" aria-hidden="true" className="shrink-0">
        <line x1="0" y1="3" x2="16" y2="3" stroke={kind === "env" ? "var(--color-fg)" : "var(--color-fg-2)"} strokeWidth={l?.width ?? 1.5} strokeDasharray={l?.dash} />
      </svg>
      {edgeKindLabel(kind)}
      {!shown && <span className="sr-only">(layer hidden)</span>}
    </span>
  );
}

function Relations({ title, groups, kinds, empty }: { title: string; groups: [string, EdgeKind[]][]; kinds: EdgeKind[]; empty: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <h5 className="label-condensed text-[13px] text-fg">{title}</h5>
      {groups.length ? (
        <ul className="flex flex-col divide-y divide-line">
          {groups.map(([svc, ks]) => (
            <li key={svc} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5">
              <span className="font-mono text-[13px] text-fg">{svc}</span>
              <span className="flex flex-wrap gap-x-3 gap-y-1">
                {ks.map((k) => <KindTag key={k} kind={k} shown={kinds.includes(k)} />)}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[13px] text-fg-2">{empty}</p>
      )}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-[12px] text-fg-2">{label}</dt>
      <dd className="min-w-0 text-[14px] text-fg">{children}</dd>
    </div>
  );
}

function group(pairs: [string, EdgeKind][]): [string, EdgeKind[]][] {
  const m = new Map<string, EdgeKind[]>();
  for (const [svc, k] of pairs) {
    if (!m.has(svc)) m.set(svc, []);
    if (!m.get(svc)!.includes(k)) m.get(svc)!.push(k);
  }
  return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/** Everything the map knows about one service, and the evidence behind each of its edges. */
export function NodeDetail({ topology, id, kinds, onClose }: { topology: Topology; id: string; kinds: EdgeKind[]; onClose: () => void }) {
  const svc = topology.services[id];
  if (!svc) return null;
  const rank = topology.criticality.findIndex((c) => c.service === id);
  const crit = topology.criticality[rank];
  const out = group(topology.edges.filter((e) => e.src === id).map((e) => [e.dst, e.kind]));
  const inc = group(topology.edges.filter((e) => e.dst === id).map((e) => [e.src, e.kind]));
  const lose = dependentsOf(topology, id, kinds).sort();
  const live = svc.container != null;

  return (
    <section aria-labelledby="map-detail-title" className="flex flex-col gap-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h4 id="map-detail-title" className="font-mono text-[20px] leading-tight text-fg">{id}</h4>
          <p className="text-[13px] text-fg-2">
            {svc.role}
            {svc.stateful && ", holds state"}
            {svc.entrypoint && ", entry point"}
            {svc.chaos_relevant ? ", can be tested" : ", exempt from testing"}
          </p>
        </div>
        <button type="button" onClick={onClose} className="inline-flex min-h-11 items-center gap-1.5 px-1 text-[13px] text-fg-2 hover:text-fg">
          <X size={14} aria-hidden="true" /> Clear selection
        </button>
      </header>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Fact label="Criticality">
          <span className="font-mono tabular text-[22px] leading-none">{(crit?.score ?? svc.criticality).toFixed(1)}</span>
          {rank >= 0 && <span className="ml-2 text-[12px] text-fg-2">rank {rank + 1} of {topology.criticality.length}</span>}
        </Fact>
        <Fact label="Memory limit">
          <span className="font-mono tabular">{svc.mem_limit_mb != null ? `${svc.mem_limit_mb}\u00a0MB` : "not set"}</span>
        </Fact>
        <Fact label="Container">
          {live ? <span className="font-mono text-[13px]">{svc.state ?? "unknown"}{svc.health ? `, ${svc.health}` : ""}</span> : <span className="text-fg-2">not inspected</span>}
        </Fact>
        <Fact label="Direct dependents">
          <span className="font-mono tabular">{crit?.fan_in ?? inc.length}</span>
        </Fact>
        <div className="col-span-2 flex min-w-0 flex-col gap-0.5 sm:col-span-4">
          <dt className="text-[12px] text-fg-2">Image</dt>
          <dd className="break-all font-mono text-[12px] text-fg">{svc.image ?? "not declared"}</dd>
        </div>
      </dl>

      {crit && crit.reasons.length > 0 && (
        <div className="flex flex-col gap-2">
          <h5 className="label-condensed text-[13px] text-fg">Why it ranks here</h5>
          <ul className="flex flex-wrap gap-1.5">
            {crit.reasons.map((r) => (
              <li key={r} className="rounded-[2px] border border-line px-2 py-1 text-[12px] text-fg">{prose(r)}</li>
            ))}
          </ul>
        </div>
      )}

      <p className="text-[14px] leading-relaxed text-fg">
        {lose.length ? (
          <>
            If <span className="font-mono">{id}</span> failed, {lose.length} {lose.length === 1 ? "service" : "services"} would lose their path under the evidence shown:{" "}
            <span className="font-mono text-[13px] text-fail">{lose.join(", ")}</span>.
          </>
        ) : (
          <>Under the evidence shown, nothing depends on <span className="font-mono">{id}</span>.</>
        )}
      </p>

      <div className="grid gap-6 md:grid-cols-2">
        <Relations title="Depends on" groups={out} kinds={kinds} empty="Nothing. No edge leaves this service." />
        <Relations title="Depended on by" groups={inc} kinds={kinds} empty="Nothing points at this service." />
      </div>
    </section>
  );
}
