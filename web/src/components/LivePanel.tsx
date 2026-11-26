import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { fmtTime, fmtTimeSec, speakerHue } from "../lib/format";
import { useAppState } from "../state/store";
import type { Line, Source } from "../types";

const STICK_THRESHOLD_PX = 48;

export function LivePanel() {
  const lines = useAppState((s) => s.live.lines);
  const state = useAppState((s) => s.recorder?.state ?? "idle");
  const speakers = useAppState((s) => s.speakers);
  // Naming a speaker must relabel lines already on screen, not only new ones.
  const names = useMemo(() => new Map(speakers.map((sp) => [sp.id, sp.display_name])), [speakers]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);
  const stickRef = useRef(true);

  // Only a user gesture can detach from the live tail; layout-driven scroll
  // events (resizes, line revisions changing heights) must not.
  const userIntentAt = useRef(0);
  const markIntent = useCallback(() => {
    userIntentAt.current = performance.now();
  }, []);

  const setStuck = useCallback((value: boolean) => {
    if (value !== stickRef.current) {
      stickRef.current = value;
      setStick(value);
    }
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX;
    if (atBottom) setStuck(true);
    else if (performance.now() - userIntentAt.current < 1000) setStuck(false);
    else if (stickRef.current) el.scrollTop = el.scrollHeight;
  }, [setStuck]);

  const jumpToLive = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setStuck(true);
  }, [setStuck]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (stickRef.current) el.scrollTop = el.scrollHeight;
    // e.g. a fresh snapshot after reconnect replaced the lines with fewer
    else if (el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX) setStuck(true);
  }, [lines, setStuck]);

  // Stay pinned to the bottom when the panel itself is resized.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (stickRef.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const recording = state === "recording" || state === "restarting" || state === "starting";

  return (
    <div className="panel live-panel">
      <div className="panel-head">
        <h2>Live transcript</h2>
        <span className="muted small">{lines.length > 0 ? `${lines.length} recent lines` : ""}</span>
      </div>
      <div
        className="live-scroll"
        ref={scrollRef}
        onScroll={onScroll}
        onWheel={markIntent}
        onTouchMove={markIntent}
        onPointerDown={markIntent}
        onKeyDown={markIntent}
        tabIndex={0}
        data-testid="live-lines"
      >
        {lines.length === 0 ? (
          <div className="empty">
            {recording ? "Listening… finalized lines will appear here." : "Not recording. Press Start to begin."}
          </div>
        ) : (
          <ol className="lines">
            {lines.map((line, i) => (
              <LineRow
                key={line.id}
                line={line}
                speaker={(line.speaker_uuid && names.get(line.speaker_uuid.toLowerCase())) || line.speaker}
                divider={i > 0 && lines[i - 1].segment_id !== line.segment_id ? line.wall : null}
              />
            ))}
          </ol>
        )}
      </div>
      {!stick && (
        <button className="btn btn-float" onClick={jumpToLive} data-testid="jump-live">
          ↓ Jump to live
        </button>
      )}
      <div className="partials">
        <PartialRow source="mic" />
        <PartialRow source="system" />
      </div>
    </div>
  );
}

const LineRow = memo(function LineRow({
  line,
  speaker,
  divider,
}: {
  line: Line;
  speaker: string;
  divider: string | null;
}) {
  const hue = speakerHue(speaker);
  return (
    <>
      {divider && (
        <li className="seg-divider" aria-label="New segment">
          <span>New segment · {fmtTime(divider)}</span>
        </li>
      )}
      <li className={`line line-${line.source}`} data-testid="live-line" data-line-id={line.id}>
        <time className="line-time tabular" dateTime={line.wall}>
          {fmtTimeSec(line.wall)}
        </time>
        <span className="speaker" style={{ "--hue": hue } as CSSProperties} title={speaker}>
          <SourceIcon source={line.source} />
          {speaker}
        </span>
        <span className="line-text">{line.text}</span>
      </li>
    </>
  );
});

const PartialRow = memo(function PartialRow({ source }: { source: Source }) {
  const partial = useAppState((s) => s.partials[source]);
  return (
    <div
      className={`partial partial-${source}`}
      data-testid={`partial-${source}`}
      data-empty={partial ? "false" : "true"}
      aria-live="off"
    >
      {partial && (
        <>
          <span className="line-time muted">…</span>
          <span className="partial-src">
            <SourceIcon source={source} />
            {partial.speaker ?? (source === "mic" ? "Microphone" : "Remote audio")}
          </span>
          <span className="partial-text">{partial.text}</span>
        </>
      )}
    </div>
  );
});

function SourceIcon({ source }: { source: Source }) {
  return (
    <span className={`src-icon src-${source}`} title={source === "mic" ? "Microphone" : "System audio"} aria-hidden="true">
      {source === "mic" ? "M" : "S"}
    </span>
  );
}
