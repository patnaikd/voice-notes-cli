import { memo, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtDuration, fmtTime } from "../lib/format";
import { useAppState, useDispatch } from "../state/store";
import type { Device, Engine, RecorderState, Settings, SourceSetting } from "../types";
import { LevelMeter } from "./LevelMeter";

const STATE_LABEL: Record<RecorderState, string> = {
  idle: "Idle",
  starting: "Starting",
  recording: "Recording",
  stopping: "Stopping",
  restarting: "Restarting",
  error: "Error",
};

const ENGINES: { value: Engine; label: string }[] = [
  { value: "fluid", label: "Fluid (speakers)" },
  { value: "apple", label: "Apple" },
  { value: "moonshine", label: "Moonshine" },
];

const SOURCES: { value: SourceSetting; label: string }[] = [
  { value: "both", label: "Mic + system" },
  { value: "mic", label: "Mic only" },
  { value: "system", label: "System only" },
];

export function RecorderBar() {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true" />
        <span className="brand-name">Voice Notes</span>
      </div>
      <RecorderControls />
      <SegmentInfo />
      <div className="meters">
        <LevelMeter source="mic" label="Mic" />
        <LevelMeter source="system" label="System" />
      </div>
      <ConnIndicator />
    </header>
  );
}

const RecorderControls = memo(function RecorderControls() {
  const recorder = useAppState((s) => s.recorder);
  const settings = useAppState((s) => s.settings);
  const dispatch = useDispatch();
  const [busy, setBusy] = useState(false);
  const state: RecorderState = recorder?.state ?? "idle";

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      const result = await fn();
      if (result && typeof result === "object" && "state" in result) {
        dispatch({ type: "recorder", recorder: result as NonNullable<typeof recorder> });
      }
    } catch {
      /* surfaced as a toast by the API client */
    } finally {
      setBusy(false);
    }
  }, [dispatch]);

  const canStart = !busy && !!recorder && state === "idle";
  const canStop = !busy && !!recorder && state !== "idle" && state !== "stopping";
  const canSplit = !busy && state === "recording";

  return (
    <div className="controls">
      <div className="btn-group">
        <button
          className="btn btn-primary"
          data-testid="start-btn"
          disabled={!canStart}
          onClick={() =>
            run(() => api.start(settings ? { engine: settings.engine, source: settings.source, mic: settings.mic } : {}))
          }
        >
          <span className="dot dot-rec" aria-hidden="true" /> Start
        </button>
        <button className="btn" data-testid="stop-btn" disabled={!canStop} onClick={() => run(api.stop)}>
          <span className="sq" aria-hidden="true" /> Stop
        </button>
        <button
          className="btn"
          data-testid="split-btn"
          disabled={!canSplit}
          onClick={() => run(api.split)}
          title="Close the current segment now and start a new one"
        >
          Split now
        </button>
      </div>
      <span
        className={`badge state state-${state}`}
        data-testid="recorder-state"
        data-state={state}
        title={recorder?.message ?? undefined}
      >
        {STATE_LABEL[state] ?? state}
      </span>
      <SettingsSelectors disabled={state !== "idle" || busy} />
    </div>
  );
});

function SettingsSelectors({ disabled }: { disabled: boolean }) {
  const settings = useAppState((s) => s.settings);
  const dispatch = useDispatch();
  const [mics, setMics] = useState<Device[] | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .devices()
      .then((d) => alive && setMics(d.microphones ?? []))
      .catch(() => alive && setMics([]));
    return () => {
      alive = false;
    };
  }, [disabled]); // refresh the device list whenever recording stops

  if (!settings) return null;

  const update = async (patch: Partial<Settings>) => {
    const next = { ...settings, ...patch };
    dispatch({ type: "settings", settings: next });
    try {
      const saved = await api.putSettings(next);
      dispatch({ type: "settings", settings: saved });
    } catch {
      dispatch({ type: "settings", settings });
    }
  };

  const micKnown = settings.mic === null || (mics ?? []).some((m) => m.name === settings.mic || m.uid === settings.mic);
  const micValue = settings.mic === null ? "" : ((mics ?? []).find((m) => m.uid === settings.mic)?.name ?? settings.mic);

  return (
    <div className="selectors">
      <label className="select">
        <span>Engine</span>
        <select
          value={settings.engine}
          disabled={disabled}
          onChange={(e) => update({ engine: e.target.value as Engine })}
          data-testid="engine-select"
          aria-label="Engine"
          title="Transcription engine"
        >
          {ENGINES.map((e) => (
            <option key={e.value} value={e.value}>
              {e.label}
            </option>
          ))}
        </select>
      </label>
      <label className="select">
        <span>Source</span>
        <select
          value={settings.source}
          disabled={disabled}
          onChange={(e) => update({ source: e.target.value as SourceSetting })}
          data-testid="source-select"
          aria-label="Source"
          title="Audio source"
        >
          {SOURCES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </label>
      <label className="select select-mic">
        <span>Mic</span>
        <select
          value={micValue}
          disabled={disabled || settings.source === "system"}
          onChange={(e) => update({ mic: e.target.value || null })}
          data-testid="mic-select"
          aria-label="Microphone"
          title="Microphone"
        >
          <option value="">System default</option>
          {!micKnown && <option value={settings.mic!}>{settings.mic}</option>}
          {(mics ?? []).map((m) => (
            <option key={m.uid} value={m.name}>
              {m.name}
              {m.default ? " (default)" : ""}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

const SegmentInfo = memo(function SegmentInfo() {
  const started = useAppState((s) => s.recorder?.segment_started_at ?? null);
  const boundary = useAppState((s) => s.recorder?.next_boundary_at ?? null);
  const state = useAppState((s) => s.recorder?.state ?? "idle");
  const active = state === "recording" || state === "restarting" || state === "starting";
  if (!active || !started) {
    return (
      <div className="seginfo seginfo-idle">
        <span className="muted">No active segment</span>
      </div>
    );
  }
  return (
    <div className="seginfo">
      <span className="seginfo-label">Segment</span>
      <span className="seginfo-value tabular">{fmtTime(started)}</span>
      {boundary && (
        <>
          <span className="seginfo-label">· next in</span>
          <Countdown to={boundary} />
        </>
      )}
    </div>
  );
});

function Countdown({ to }: { to: string }) {
  const target = new Date(to).getTime();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const remaining = Number.isNaN(target) ? 0 : (target - now) / 1000;
  return (
    <span className="seginfo-value tabular" data-testid="segment-countdown" title={`Boundary at ${fmtTime(to)}`}>
      {fmtDuration(remaining)}
    </span>
  );
}

const ConnIndicator = memo(function ConnIndicator() {
  const conn = useAppState((s) => s.conn);
  const label = conn === "live" ? "Live" : conn === "connecting" ? "Connecting" : "Reconnecting";
  return (
    <span className={`conn conn-${conn}`} data-testid="conn-status" data-status={conn} title={`Server connection: ${label}`}>
      <span className="conn-dot" aria-hidden="true" />
      {label}
    </span>
  );
});
