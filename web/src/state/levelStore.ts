import type { Levels, Source } from "../types";

/**
 * Audio levels arrive at up to 10 Hz. They bypass React state entirely: the
 * meters read from here inside a requestAnimationFrame loop.
 */
export interface LevelSample {
  rms: number;
  peak: number;
  at: number; // performance.now() of arrival
}

type Listener = () => void;

const latest: Record<Source, LevelSample | null> = { mic: null, system: null };
const listeners = new Set<Listener>();

export const levelStore = {
  set(levels: Levels) {
    const now = performance.now();
    if (levels.mic) latest.mic = { ...levels.mic, at: now };
    if (levels.system) latest.system = { ...levels.system, at: now };
    for (const l of listeners) l();
  },
  get(source: Source): LevelSample | null {
    return latest[source];
  },
  /** Called on every new sample; meters use it to (re)start their rAF loop. */
  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  reset() {
    latest.mic = null;
    latest.system = null;
  },
};

/** Linear amplitude 0..1 to a 0..1 meter fraction on a -60..0 dB scale. */
export function toMeterFraction(linear: number): number {
  if (!(linear > 0)) return 0;
  const db = 20 * Math.log10(linear);
  if (db <= -60) return 0;
  if (db >= 0) return 1;
  return (db + 60) / 60;
}

export function toDb(linear: number): number {
  if (!(linear > 0)) return -Infinity;
  return Math.max(-60, Math.min(0, 20 * Math.log10(linear)));
}
