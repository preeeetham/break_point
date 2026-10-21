import { useRef } from "react";
import { motion, useInView, useReducedMotion } from "motion/react";
import { useConnection } from "../lib/store";
import { Button, Container } from "../components/ui";
import { useStartWalkthrough } from "./nav";

const BANKS = 3;

/** The lights go down: the band is already the dark act underneath, and three banks of lab light
 *  switch off over it, top to bottom, the first time it comes into view. Opacity only. */
export function Handoff() {
  const start = useStartWalkthrough();
  const conn = useConnection();
  const reduce = useReducedMotion();
  const ref = useRef<HTMLDivElement>(null);
  const seen = useInView(ref, { once: true, amount: 0.4 });

  return (
    <div ref={ref} className="relative">
      <section data-theme="camera" aria-labelledby="handoff-title" className="bg-bg text-fg">
        <Container className="py-20 md:py-24">
          <h2 id="handoff-title" className="display text-balance text-[clamp(2rem,4.6vw,3.75rem)] text-fg">
            Now run it, one station at a time.
          </h2>
          <p className="mt-5 max-w-[54ch] text-[17px] leading-relaxed text-fg-2">
            Each station shows what goes in, what its agent does and what comes out. Stop everything stays in the header the whole way.
            {conn === "replay" && " The server is not reachable right now, so the stations show a recorded run."}
          </p>
          <Button className="mt-9" onClick={start}>Start the walkthrough</Button>
        </Container>
      </section>
      {!reduce && (
        <div aria-hidden="true" className="pointer-events-none absolute inset-0 flex flex-col">
          {Array.from({ length: BANKS }, (_, i) => (
            <motion.div
              key={i}
              className="flex-1 bg-bg"
              initial={{ opacity: 1 }}
              animate={{ opacity: seen ? 0 : 1 }}
              transition={{ duration: 0.16, delay: seen ? 0.12 + i * 0.26 : 0, ease: "easeOut" }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
