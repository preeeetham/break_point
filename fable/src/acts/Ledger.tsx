import { AGENT_LEDGER, STAGES } from "../lib/stages";
import { AiMark, Container } from "../components/ui";

const isNothing = (model: string) => model.startsWith("Nothing");

function stationOf(stage: (typeof AGENT_LEDGER)[number]["stage"]): string {
  if (!stage) return "Every station, in the feed";
  const d = STAGES.find((x) => x.id === stage);
  return d ? `Station ${d.n}, ${d.name}` : "";
}

export function Ledger() {
  const total = AGENT_LEDGER.length;
  const withModel = AGENT_LEDGER.filter((a) => !isNothing(a.model)).length;
  return (
    <section id="the-rule" aria-labelledby="rule-title" className="border-t border-line">
      <Container className="grid gap-12 py-20 md:py-28 lg:grid-cols-[minmax(16rem,22rem)_minmax(0,1fr)] lg:gap-16">
        <div className="lg:sticky lg:top-10 lg:self-start">
          <h2 id="rule-title" className="display text-balance text-[clamp(2rem,3.5vw,3.1rem)] text-fg">
            The model proposes. Code decides.
          </h2>
          <p className="mt-5 max-w-[46ch] text-[17px] leading-relaxed text-fg-2">
            Code makes every decision that runs, scores or stops a test, and the model writes only in the lane it is given.
          </p>
          <p className="mt-6 max-w-[46ch] text-[14px] leading-relaxed text-fg-2">
            <span className="mr-1.5 inline-block align-[1px]"><AiMark /></span>
            marks a model lane. {withModel} of the {total} agents have one; the other {total - withModel} are code only.
          </p>
        </div>

        <div className="min-w-0">
          <div aria-hidden="true" className="hidden grid-cols-[10.5rem_minmax(0,1fr)_minmax(0,1fr)] gap-x-8 border-b border-fg pb-3 md:grid">
            <span className="label-condensed text-[14px] text-fg">Agent</span>
            <span className="label-condensed text-[14px] text-fg">Code decides</span>
            <span className="label-condensed text-[14px] text-model">The model may</span>
          </div>
          <ul className="divide-y divide-line border-b border-line md:border-b-0">
            {AGENT_LEDGER.map((a) => {
              const quiet = isNothing(a.model);
              const [head, ...rest] = a.model.split(". ");
              return (
                <li key={a.name} className="grid gap-x-8 gap-y-3 py-5 md:grid-cols-[10.5rem_minmax(0,1fr)_minmax(0,1fr)]">
                  <div>
                    <p className="display text-[19px] text-fg">{a.name}</p>
                    <p className="mt-0.5 text-[12px] text-fg-2">{stationOf(a.stage)}</p>
                  </div>
                  <div>
                    <p className="label-condensed text-[13px] text-fg md:sr-only">Code decides</p>
                    <p className="text-[15px] leading-snug text-fg">{a.code}</p>
                  </div>
                  {quiet ? (
                    <div className="text-fg-2">
                      <p className="label-condensed text-[13px] md:sr-only">The model may</p>
                      <p className="text-[14px] leading-snug">
                        <span className="text-fg-2">{head}.</span>
                        {rest.length > 0 && <span className="text-fg-2"> {rest.join(". ")}</span>}
                      </p>
                    </div>
                  ) : (
                    <div className="text-model">
                      <p className="label-condensed text-[13px] md:sr-only">The model may</p>
                      <p className="flex items-start gap-2 text-[15px] leading-snug">
                        <span className="mt-0.5 shrink-0"><AiMark /></span>
                        <span>{a.model}</span>
                      </p>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      </Container>
    </section>
  );
}
