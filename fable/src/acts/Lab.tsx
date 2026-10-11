import { Hero } from "./Hero";
import { Sequence } from "./Sequence";
import { Ledger } from "./Ledger";
import { Barrier } from "./Barrier";
import { Handoff } from "./Handoff";
import { Proof } from "./Proof";

/** The light act: what Faultline is, in four screens, then the hand-off into the dark workflow. */
export function Lab() {
  return (
    <div id="top" className="bg-bg text-fg">
      <Hero />
      <Sequence />
      <Ledger />
      <Barrier />
      <Proof />
      <Handoff />
    </div>
  );
}
