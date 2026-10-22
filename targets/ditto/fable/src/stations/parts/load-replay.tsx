import { useEffect, useRef, useState } from "react";
import { actions } from "../../lib/store";
import type { EventItem, OnboardingState } from "../../lib/types";

interface Played {
  lines: EventItem[];
  onboarding: OnboardingState | null;
}

// survives a station switch, so a finished playback is not replayed on every visit
let played: Played | null = null;

const MIN_GAP_MS = 220;
const MAX_GAP_MS = 3600;

/**
 * Plays the recorded onboarding of eclipse-ditto/ditto back line by line, paced by the gaps between
 * the recorded timestamps (the model call really took 3.6 s), with a floor so a burst stays legible.
 */
export function useReplayPlayback() {
  const [lines, setLines] = useState<EventItem[]>(played?.lines ?? []);
  const [result, setResult] = useState<OnboardingState | null>(played?.onboarding ?? null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timers = useRef<number[]>([]);

  useEffect(() => () => timers.current.forEach((t) => window.clearTimeout(t)), []);

  async function play() {
    timers.current.forEach((t) => window.clearTimeout(t));
    timers.current = [];
    setLines([]);
    setResult(null);
    setError(null);
    setPlaying(true);
    const rec = await actions.replayOnboarding();
    if (!rec || !rec.events?.length) {
      setPlaying(false);
      setError("The recorded run could not be loaded from replay/onboarding.json.");
      return;
    }
    const evs = rec.events;
    let at = 350;
    evs.forEach((e, i) => {
      const gap = i === 0 ? 0 : (e.ts - evs[i - 1].ts) * 1000;
      at += Math.min(MAX_GAP_MS, Math.max(MIN_GAP_MS, gap));
      timers.current.push(window.setTimeout(() => setLines(evs.slice(0, i + 1)), at));
    });
    timers.current.push(
      window.setTimeout(() => {
        setResult(rec.onboarding);
        setPlaying(false);
        played = { lines: evs, onboarding: rec.onboarding };
      }, at + 450),
    );
  }

  return { lines, result, playing, error, play };
}
