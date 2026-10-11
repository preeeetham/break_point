import { useId } from "react";
import { STAGES, type StageDef } from "../lib/stages";
import { useFState } from "../lib/store";
import { MarkerGlyph } from "../components/Marker";
import { Container, cx } from "../components/ui";
import { useOpenStation } from "./nav";

export function Sequence() {
  const s = useFState();
  const loaded = !!s?.topology;
  return (
    <section id="how-it-works" aria-labelledby="how-title" className="border-t border-line">
      <Container className="py-20 md:py-28">
        <h2 id="how-title" className="display max-w-[20ch] text-balance text-[clamp(2rem,4.2vw,3.5rem)] text-fg">
          Eight stations, each feeding the next.
        </h2>
        <p className="mt-5 max-w-[56ch] text-[17px] leading-relaxed text-fg-2">
          Each station is one job, done by the agents named under it. Open one to run it, or to read what it found last time.
        </p>
        <ol className="mt-14 hidden xl:grid xl:grid-cols-8">
          {STAGES.map((d) => (
            <TrackStop key={d.id} d={d} loaded={loaded} />
          ))}
        </ol>
        <ol className="mt-12 flex flex-col xl:hidden">
          {STAGES.map((d, i) => (
            <ListStop key={d.id} d={d} loaded={loaded} last={i === STAGES.length - 1} />
          ))}
        </ol>
      </Container>
    </section>
  );
}

function LoadNote({ loaded }: { loaded: boolean }) {
  return (
    <span className="mt-4 block text-[13px] leading-snug text-fg">
      For an unfamiliar repo.{loaded && " Ditto is already loaded, so you can start at Map."}
    </span>
  );
}

/** Desktop: a horizontal test track. Load's segment is dashed because it is optional for Ditto;
 *  the marker sits on Break, where the fault goes in. */
function TrackStop({ d, loaded }: { d: StageDef; loaded: boolean }) {
  const open = useOpenStation();
  const lineId = useId();
  const optional = d.id === "load";
  return (
    <li className="min-w-0">
      <button
        type="button"
        onClick={() => open(d.id)}
        aria-label={`Open station ${d.n}, ${d.name}`}
        aria-describedby={lineId}
        className="group flex h-full w-full flex-col pb-6 text-left transition-colors duration-150 hover:bg-sheet"
      >
        <span className="display-wide block px-4 pb-5 pt-3 text-[44px] leading-none text-fg tabular">{d.n}</span>
        <span aria-hidden="true" className={cx("relative block h-0 w-full border-t border-fg", optional && "border-dashed")}>
          {d.id === "break" ? (
            <span className="absolute left-4 top-0 -translate-x-1/2 -translate-y-1/2">
              <MarkerGlyph size={20} />
            </span>
          ) : (
            <span className="absolute left-4 top-0 h-3 w-px -translate-y-1/2 bg-fg" />
          )}
        </span>
        <span className="block px-4 pt-6">
          <span className="display block text-[22px] text-fg decoration-1 underline-offset-4 group-hover:underline">{d.name}</span>
          <span className="mt-1 block text-[13px] leading-snug text-fg-2">{d.agentNames}</span>
          <span id={lineId} className="mt-4 block text-[14px] leading-snug text-fg-2">{d.line}</span>
          {optional && <LoadNote loaded={loaded} />}
        </span>
      </button>
    </li>
  );
}

/** Below 1280 px: the same track, turned vertical. */
function ListStop({ d, loaded, last }: { d: StageDef; loaded: boolean; last: boolean }) {
  const open = useOpenStation();
  const lineId = useId();
  const optional = d.id === "load";
  return (
    <li>
      <button
        type="button"
        onClick={() => open(d.id)}
        aria-label={`Open station ${d.n}, ${d.name}`}
        aria-describedby={lineId}
        className="group grid w-full grid-cols-[2.75rem_minmax(0,1fr)] gap-x-4 text-left transition-colors duration-150 hover:bg-sheet md:grid-cols-[3.5rem_minmax(0,11rem)_minmax(0,1fr)] md:gap-x-6"
      >
        <span aria-hidden="true" className="relative flex flex-col items-center self-stretch pt-4">
          <span className="display-wide relative z-10 bg-bg px-1 text-[32px] leading-none text-fg tabular group-hover:bg-sheet md:text-[38px]">{d.n}</span>
          {!last && <span className={cx("mt-3 block w-0 flex-1 border-l border-fg", optional && "border-dashed")} />}
          {d.id === "break" && (
            <span className="absolute left-1/2 top-[58px] -translate-x-1/2 md:top-[64px]">
              <MarkerGlyph size={18} />
            </span>
          )}
        </span>
        <span className="block pb-8 pt-4 md:pb-10">
          <span className="display block text-[22px] text-fg decoration-1 underline-offset-4 group-hover:underline">{d.name}</span>
          <span className="mt-1 block text-[13px] leading-snug text-fg-2">{d.agentNames}</span>
          <span className="block md:hidden">
            <span className="mt-3 block max-w-[52ch] text-[15px] leading-snug text-fg-2">{d.line}</span>
            {optional && <LoadNote loaded={loaded} />}
          </span>
        </span>
        <span id={lineId} className="hidden pb-10 pt-5 md:block">
          <span className="block max-w-[56ch] text-[15px] leading-snug text-fg-2">{d.line}</span>
          {optional && <LoadNote loaded={loaded} />}
        </span>
      </button>
    </li>
  );
}
