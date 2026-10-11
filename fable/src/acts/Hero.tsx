import { MarkerGlyph } from "../components/Marker";
import { Button, Container } from "../components/ui";
import { Rig } from "./Rig";
import { useStartWalkthrough } from "./nav";

export function Hero() {
  const start = useStartWalkthrough();
  return (
    <section aria-labelledby="hero-title" className="pb-20 md:pb-28">
      <Container>
        <div className="flex h-16 items-center gap-2.5">
          <MarkerGlyph size={20} />
          <span className="display text-[18px] text-fg">Faultline</span>
        </div>
        <h1 id="hero-title" className="display-wide mt-6 text-[clamp(2.6rem,6.1vw,5.6rem)] text-fg md:mt-8">
          <span className="block">Crash-test your cloud app.</span>
          <span className="block">On purpose.</span>
        </h1>
        <div className="mt-8 grid gap-10 md:mt-10 lg:grid-cols-[minmax(0,4fr)_minmax(0,8fr)] lg:gap-10 xl:grid-cols-[minmax(0,3fr)_minmax(0,9fr)]">
          <div className="flex flex-col gap-7">
            <p className="max-w-[36ch] text-[17px] leading-relaxed text-fg-2 md:text-[19px] xl:text-[18px]">
              Agents map a live Eclipse Ditto stack, predict where it will break, then break it inside limits the AI cannot touch.
            </p>
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
              <Button onClick={start}>Start the walkthrough</Button>
              <a
                href="#how-it-works"
                className="inline-flex min-h-11 items-center text-[15px] font-medium text-fg underline decoration-fg-2 decoration-1 underline-offset-[6px] hover:decoration-fg"
              >
                How it works
              </a>
            </div>
          </div>
          <Rig />
        </div>
      </Container>
    </section>
  );
}
