// The Narrator's templated play-by-play for one experiment, set as a quiet timeline.
import { AiMark } from "../../components/ui";
import { Rich } from "./score-marks";
import { tidy } from "./score-text";
import { useNarrative } from "./score-data";

const STAMP = /^\[(\+?[\d.:]+)s?\]\s*(.*)$/;

function split(line: string): { at: string; text: string } {
  const m = tidy(line).match(STAMP);
  if (!m) return { at: "", text: tidy(line) };
  const raw = m[1];
  return { at: raw.startsWith("+") ? `t${raw}\u00a0s` : raw, text: m[2] };
}

/** riskByModel: the Narrator quotes the stored risk summary, which the model may have rewritten. */
export function NarrativeTimeline({ eid, riskByModel = false }: { eid: string; riskByModel?: boolean }) {
  const n = useNarrative(eid);
  if (n.status === "loading") return <p className="text-[13px] text-fg-2">Reading the Narrator's account…</p>;
  if (n.status === "missing") return <p className="text-[13px] text-fg-2">The Narrator has no account of this experiment. The server may not have its report.</p>;
  const d = n.data;
  const head = split(d.header);
  const lines = [d.impact, d.recovery, d.hypothesis, d.safety, ...d.findings].filter((x): x is string => !!x);
  return (
    <div className="flex flex-col gap-4 text-[13px] leading-relaxed text-fg-2">
      <ol className="flex flex-col">
        {[head, ...d.timeline.map(split)].map((t, i) => (
          <li key={i} className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-x-3">
            <span className="py-1 font-mono tabular text-[12px]">{t.at}</span>
            <Rich text={t.text} className={i === 0 ? "py-1 text-fg" : "break-words border-l border-line py-1 pl-3"} />
          </li>
        ))}
      </ol>
      <ul className="flex max-w-[78ch] flex-col gap-1.5">
        {lines.map((l, i) => (
          <li key={i} className="break-words"><Rich text={l} /></li>
        ))}
        {d.risk && (
          <li className={riskByModel ? "break-words text-model" : "break-words"}>
            {riskByModel && <AiMark />} <Rich text={d.risk} />
          </li>
        )}
        {d.fix && <li className="break-words"><Rich text={d.fix} /></li>}
      </ul>
      {d.error && <p className="text-fail">{tidy(d.error)}</p>}
      {d.llm && (
        <p className="flex max-w-[78ch] flex-col gap-1.5 text-model">
          <AiMark>AI closing note</AiMark>
          <span>{tidy(d.llm)}</span>
        </p>
      )}
    </div>
  );
}
