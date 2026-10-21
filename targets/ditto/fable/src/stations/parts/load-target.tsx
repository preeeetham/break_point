import { useState } from "react";
import { WarningCircle } from "@phosphor-icons/react";
import { actions } from "../../lib/store";
import type { OnboardingReport, State } from "../../lib/types";
import { announce } from "../../lib/a11y";
import { Notice } from "../../components/Station";
import { Button } from "../../components/ui";
import { adoptable, repoRel, stripExc } from "./load-util";

function PathRows({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-fg-2">{k}</dt>
          <dd className="break-all font-mono text-[12px] leading-5 text-fg">{v || <span className="text-fg-2">not set</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

function adoptFix(error: string): string {
  if (/must live under/i.test(error)) return "The control room only adopts files under deploy/, config/ or .cache/onboarding/. Load the repository again from this station: it writes to .cache/onboarding/knowledge.";
  if (/not found/i.test(error)) return "The file is no longer on disk. Load the repository again to regenerate it.";
  if (/replay/i.test(error)) return "Start the server with faultline dashboard --port 8777, then adopt the target.";
  return "Check both paths above, then try again.";
}

/** The two ways out of this station: point the room at what was just loaded, or leave the target alone. */
export function NextSteps({ report, target, root, recorded, onDone }: {
  report: OnboardingReport;
  target: State["target"];
  root: string | null;
  recorded: boolean;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const compose = repoRel(report.compose_path, root);
  const knowledge = repoRel(report.output_path, root);
  const blocked = [compose, knowledge].filter((p) => p && !adoptable(p));

  async function adopt() {
    setBusy(true);
    setError(null);
    const r = await actions.adoptTarget(report.compose_path, report.output_path);
    setBusy(false);
    if (r.ok) {
      announce(`Target adopted: ${report.slug}. Map the system to draw it.`);
      onDone();
    } else {
      setError(r.error ?? "The server refused the target.");
    }
  }

  return (
    <section aria-labelledby="load-next" className="flex flex-col gap-4 border-t border-line pt-8">
      <h3 id="load-next" className="display text-[22px] text-fg">Choose what to test next</h3>
      <div className="grid gap-8 md:grid-cols-2 md:gap-0 md:divide-x md:divide-line">
        <div className="flex flex-col gap-4 md:pr-8">
          <div className="flex flex-col gap-1.5">
            <h4 className="label-condensed text-[15px] text-fg">Use {report.slug} as the target</h4>
            <p className="max-w-[52ch] text-[14px] leading-relaxed text-fg-2">
              The control room disarms, points at these two files, and the next map is drawn from them.
            </p>
          </div>
          <PathRows rows={[["Compose", compose], ["Knowledge", knowledge]]} />
          {blocked.length > 0 && (
            <p className="flex items-start gap-2 text-[13px] leading-snug text-fg">
              <WarningCircle size={14} weight="fill" className="mt-[2px] shrink-0 text-fail" aria-hidden="true" />
              <span>
                <span className="font-mono">{blocked[0]}</span> is outside deploy/, config/ and .cache/onboarding/, so the server will refuse it.
                Load the repository again here to write the file to .cache/onboarding/knowledge.
              </span>
            </p>
          )}
          {recorded && <p className="text-[12px] text-fg-2">This result is a recording. Adopting it needs the live server.</p>}
          <div>
            <Button onClick={adopt} disabled={busy} aria-describedby={error ? "adopt-error" : undefined}>
              Use this repo as the target
            </Button>
          </div>
          {error && (
            <div id="adopt-error">
              <Notice tone="fail">
                Refused: {stripExc(error)}
                <span className="mt-1 block text-fg">{adoptFix(error)}</span>
              </Notice>
            </div>
          )}
        </div>
        <div className="flex flex-col gap-4 border-t border-line pt-8 md:border-t-0 md:pl-8 md:pt-0">
          <div className="flex flex-col gap-1.5">
            <h4 className="label-condensed text-[15px] text-fg">Keep the current target</h4>
            <p className="max-w-[52ch] text-[14px] leading-relaxed text-fg-2">
              Nothing changes. The map and every experiment keep using these files.
            </p>
          </div>
          <PathRows rows={[["Compose", repoRel(target?.compose_file, root)], ["Knowledge", repoRel(target?.knowledge_file, root)], ["Project", target?.project ?? ""]]} />
          <div>
            <Button variant="ghost" onClick={onDone}>Keep the current target</Button>
          </div>
        </div>
      </div>
    </section>
  );
}
