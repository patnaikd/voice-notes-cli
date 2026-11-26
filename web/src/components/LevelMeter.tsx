import { memo, useEffect, useRef } from "react";
import { levelStore, toDb, toMeterFraction } from "../state/levelStore";
import type { Source } from "../types";

const STALE_MS = 1500;
const RELEASE_PER_S = 1.4; // meter fraction per second
const PEAK_HOLD_MS = 900;
const PEAK_RELEASE_PER_S = 0.5;

/**
 * A level meter on a -60..0 dB scale. Values come from `levelStore` and are
 * written straight to the DOM in a requestAnimationFrame loop, so 10 Hz level
 * messages never cause React renders. The loop stops when the meter settles.
 */
export const LevelMeter = memo(function LevelMeter({ source, label }: { source: Source; label: string }) {
  const coverRef = useRef<HTMLDivElement>(null);
  const peakRef = useRef<HTMLDivElement>(null);
  const dbRef = useRef<HTMLSpanElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let raf = 0;
    let display = 0;
    let peak = 0;
    let peakAt = 0;
    let last = performance.now();
    let lastText = 0;
    let shownText = "";

    const tick = (now: number) => {
      raf = 0;
      const dt = Math.min(0.1, Math.max(0, (now - last) / 1000));
      last = now;
      const sample = levelStore.get(source);
      const fresh = sample !== null && now - sample.at < STALE_MS;
      const target = fresh ? toMeterFraction(sample!.rms) : 0;
      const peakTarget = fresh ? toMeterFraction(sample!.peak) : 0;

      display = target > display ? display + (target - display) * 0.5 : Math.max(target, display - RELEASE_PER_S * dt);
      if (peakTarget >= peak) {
        peak = peakTarget;
        peakAt = now;
      } else if (now - peakAt > PEAK_HOLD_MS) {
        peak = Math.max(peakTarget, display, peak - PEAK_RELEASE_PER_S * dt);
      }

      if (coverRef.current) coverRef.current.style.transform = `scaleX(${(1 - display).toFixed(4)})`;
      if (peakRef.current) {
        peakRef.current.style.transform = `translateX(${(peak * 100).toFixed(2)}%)`;
        peakRef.current.style.opacity = peak > 0.005 ? "1" : "0";
      }
      if (now - lastText > 250) {
        lastText = now;
        const text = fresh ? `${Math.round(Math.max(-60, toDb(sample!.rms)))} dB` : "–";
        if (text !== shownText && dbRef.current) {
          dbRef.current.textContent = text;
          shownText = text;
        }
        if (rootRef.current) rootRef.current.dataset.active = fresh ? "true" : "false";
      }

      const settled = !fresh && display < 0.001 && peak < 0.001;
      if (!settled) raf = requestAnimationFrame(tick);
      else if (dbRef.current && shownText !== "–") {
        dbRef.current.textContent = "–";
        shownText = "–";
      }
    };

    const kick = () => {
      if (!raf) {
        last = performance.now();
        raf = requestAnimationFrame(tick);
      }
    };
    const unsubscribe = levelStore.subscribe(kick);
    kick();
    return () => {
      unsubscribe();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [source]);

  return (
    <div className="meter" ref={rootRef} data-testid={`level-${source}`} data-active="false">
      <span className="meter-label">{label}</span>
      <div className="meter-track" aria-hidden="true">
        <div className="meter-cover" ref={coverRef} />
        <div className="meter-peak" ref={peakRef} />
        <div className="meter-ticks" />
      </div>
      <span className="meter-db" ref={dbRef}>
        –
      </span>
    </div>
  );
});
