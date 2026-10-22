import { useState } from "react";
import { WarningCircle, WarningDiamond } from "@phosphor-icons/react";
import { Instrument, Notice, Station } from "../components/Station";
import { EdgeLegend, TopologyMap } from "../components/TopologyMap";
import { Button } from "../components/ui";
import { useConnection, useFState } from "../lib/store";
import { AGENT_LEDGER } from "../lib/stages";
import { clock } from "../lib/format";
import type { EdgeKind } from "../lib/types";
import { MapFrame, Ticks, Working } from "./parts/load-shared";
import { prose, stripExc, useStationJob } from "./parts/load-util";
import { LayerToggles, PathLedger } from "./parts/map-layers";
import { NodeDetail } from "./parts/map-detail";

const LEDGER = AGENT_LEDGER.find((a) => a.name === "Topology")!;
const SKIPPED = /^live enrichment skipped/i;
const DOCKER_DOWN = /docker|socket|connection (refused|aborted)|Error while fetching server API version/i;

/** "mongodb: not referenced ..." -> service name in mono, the rest as prose */
function Finding({ text }: { text: string }) {
  const m = /^([a-z0-9][\w.-]*):\s+(.*)$/i.exec(prose(text));
  return (
    <li className="flex items-start gap-2.5 text-[14px] leading-snug text-fg">
      <WarningDiamond size={16} weight="fill" className="mt-[2px] shrink-0 text-fg" aria-hidden="true" />
      <span>
        <span className="sr-only">Finding: </span>
        {m ? <><span className="font-mono">{m[1]}</span>: <Ticks text={m[2]} /></> : <Ticks text={text} />}
      </span>
    </li>
  );
}

export function MapStation() {
  const s = useFState();
  const replay = useConnection() === "replay";
  const job = useStationJob("topology");
  const [kinds, setKinds] = useState<EdgeKind[]>(["depends_on"]);
  const [selected, setSelected] = useState<string | null>(null);
  const [lastLive, setLastLive] = useState<boolean | null>(null);

  const topo = s?.topology ?? null;
  const findings = topo?.findings ?? [];
  const skipped = findings.find((f) => SKIPPED.test(f)) ?? null;
  const structural = findings.filter((f) => !SKIPPED.test(f));
  const live = !!topo && Object.values(topo.services).some((x) => x.container != null);
  const dockerFailed = !!job.failed && DOCKER_DOWN.test(job.failed);
  const offerStatic = !job.busy && (dockerFailed || !!skipped);
  const targetChanged = s?.agents.topology?.detail === "target changed";
  const top = topo?.criticality.find((c) => topo.services[c.service]?.chaos_relevant) ?? topo?.criticality[0];

  function map(liveLayer: boolean) {
    setLastLive(liveLayer);
    job.start(liveLayer ? {} : { live: false });
  }
  const select = (id: string) => setSelected((cur) => (cur === id ? null : id));

  const controls = (
    <>
      <div className="flex flex-wrap gap-3">
        <Button onClick={() => map(true)} disabled={job.busy || !!job.other}>Map the system</Button>
      </div>
      {job.busy && (
        <Working>
          {lastLive === false ? "Mapping system from files: reading compose, scanning environment, parsing nginx…" : "Mapping system: reading compose, scanning environment, inspecting containers…"}
        </Working>
      )}
      {job.other && <Notice>{job.other.label} is running. Map the system is available when it finishes.</Notice>}
      {job.refusal && <Notice tone="fail">Refused: {stripExc(job.refusal)}</Notice>}
      {job.failed && !job.busy && !dockerFailed && (
        <Notice tone="fail" live={job.mine}>
          Mapping failed: {stripExc(job.failed)}
          <span className="mt-1 block text-fg">Check that the compose file the target points at still exists and parses, then map again.</span>
        </Notice>
      )}
      {offerStatic && (
        <div className="flex flex-col gap-3 border-l-2 border-fail/60 pl-3">
          <p role={dockerFailed && job.mine ? "alert" : undefined} className="text-[13px] leading-snug text-fg">
            <WarningCircle size={14} weight="fill" aria-hidden="true" className="mr-1.5 inline-block align-[-2px] text-fail" />
            <span className="text-fail">Docker is not reachable,</span> so the live layer is missing.
            {dockerFailed ? ` ${stripExc(job.failed)}.` : skipped ? <span className="text-fg-2"> ({prose(skipped).replace(/^live enrichment skipped:\s*/i, "")})</span> : null}
          </p>
          <p className="text-[13px] leading-snug text-fg-2">
            Files only reads compose depends_on, environment variables, nginx routes and the knowledge file. It skips docker inspect, so
            container state and health stay blank. The edges and the ranking are the same. Start Docker and map again to add the live layer.
          </p>
          <div>
            <Button variant="ghost" onClick={() => map(false)} disabled={job.busy || !!job.other}>Map from files only</Button>
          </div>
        </div>
      )}
      {targetChanged && !job.busy && (
        <Notice>The target changed. The map below may belong to the previous target: map the system to redraw it.</Notice>
      )}
      {replay && <Notice>Replay: this is the recorded map. Mapping needs the live server.</Notice>}
      {topo && !job.busy && top && (
        <Notice tone="pass">
          System mapped{live ? " with the live layer" : " from files"}. <span className="font-mono">{top.service}</span> ranked first: {prose(top.reasons[0] ?? "")}.
        </Notice>
      )}
    </>
  );

  const instrument = topo ? (
    <Instrument
      title="Dependency map"
      meta={`${Object.keys(topo.services).length} services, ${topo.edges.length} edges, ${live ? "files and docker" : "files only"}, ${clock(topo.generated_at)}`}
    >
      <div className="flex flex-col gap-5">
        <LayerToggles topology={topo} kinds={kinds} onChange={setKinds} />
        {structural.length > 0 && (
          <ul aria-label="Findings from the map" className="flex flex-col gap-2 border-l-2 border-fg pl-3">
            {structural.map((f) => <Finding key={f} text={f} />)}
          </ul>
        )}
        <PathLedger topology={topo} kinds={kinds} selected={selected} onSelect={select} />
        <div className="flex min-w-0 flex-col gap-3">
          <MapFrame>
            <TopologyMap
              topology={topo}
              halo="var(--color-sheet)"
              kinds={kinds}
              ariaLabel={`Dependency map of ${Object.keys(topo.services).length} services, drawn with ${kinds.length} of 4 evidence layers`}
              selected={selected}
              onSelect={select}
              blastFrom={selected}
              height={360}
            />
          </MapFrame>
          <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
            <EdgeLegend kinds={kinds.length ? kinds : ["depends_on"]} />
            <p className="text-[12px] leading-snug text-fg-2">
              Marker size is criticality. {selected ? <>Red rings would lose their path if <span className="font-mono">{selected}</span> failed.</> : "Select a service to see what it takes down with it."}
            </p>
          </div>
        </div>
        <div className="border-t border-line pt-5">
          {selected ? (
            <NodeDetail topology={topo} id={selected} kinds={kinds} onClose={() => setSelected(null)} />
          ) : (
            <p className="text-[14px] text-fg-2">Select a service on the map or in the ranking to see its role, image, limits and the evidence behind each edge.</p>
          )}
        </div>
      </div>
    </Instrument>
  ) : (
    <Instrument title="Dependency map">
      <p className="py-10 text-[15px] text-fg-2">No map yet. Map the system to draw the dependency graph.</p>
    </Instrument>
  );

  return (
    <Station
      id="map"
      headline="The dependency compose never declared"
      lede="The Topology agent reads compose depends_on, environment variables, nginx routes, the knowledge file and live docker inspect, then ranks every service by how much breaks if it fails. No model is involved."
      code={LEDGER.code}
      controls={controls}
      instrument={instrument}
    />
  );
}
