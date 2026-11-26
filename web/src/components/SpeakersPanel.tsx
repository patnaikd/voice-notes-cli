import { memo, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { fmtDateTime } from "../lib/format";
import { useAppState, useDispatch } from "../state/store";
import type { Speaker } from "../types";
import { ConfirmDialog } from "./ConfirmDialog";

type Pending = { kind: "forget"; speaker: Speaker } | { kind: "merge"; source: Speaker; target: Speaker };

export function SpeakersPanel() {
  const speakers = useAppState((s) => s.speakers);
  const dispatch = useDispatch();
  const [pending, setPending] = useState<Pending | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);

  const sorted = [...speakers].sort((a, b) => {
    // Unnamed speakers first: they are the ones that need attention.
    if (!a.name !== !b.name) return a.name ? 1 : -1;
    return a.display_name.localeCompare(b.display_name);
  });

  const refresh = async () => {
    try {
      dispatch({ type: "speakers", speakers: await api.speakers() });
    } catch {
      /* toast */
    }
  };

  const confirm = async () => {
    const p = pending;
    setPending(null);
    if (!p) return;
    try {
      if (p.kind === "forget") {
        await api.forgetSpeaker(p.speaker.id);
        dispatch({ type: "toast", level: "info", message: `Forgot ${p.speaker.display_name}` });
      } else {
        await api.mergeSpeakers(p.source.id, p.target.id);
        dispatch({
          type: "toast",
          level: "info",
          message: `Merged ${p.source.display_name} into ${p.target.display_name}`,
        });
      }
      await refresh();
    } catch {
      /* toast */
    }
  };

  if (speakers.length === 0) {
    return (
      <div className="empty">
        No speakers yet. With the Fluid engine, voices are enrolled automatically; name them here once they appear.
      </div>
    );
  }

  return (
    <div className="speakers">
      <p className="muted small">
        Listen to a sample, then name the speaker. Naming or merging corrects past transcripts.
      </p>
      <ul className="speaker-list">
        {sorted.map((sp) => (
          <SpeakerRow
            key={sp.id}
            speaker={sp}
            others={speakers}
            playing={playing === sp.id}
            onPlay={(on) => setPlaying(on ? sp.id : null)}
            onForget={() => setPending({ kind: "forget", speaker: sp })}
            onMerge={(target) => setPending({ kind: "merge", source: sp, target })}
            onRenamed={refresh}
          />
        ))}
      </ul>
      {pending?.kind === "forget" && (
        <ConfirmDialog
          title={`Forget ${pending.speaker.display_name}?`}
          body="The voice profile and sample are deleted. Past transcripts keep their text, and this voice will be enrolled again as a new unknown speaker if heard."
          confirmLabel="Forget speaker"
          danger
          onConfirm={confirm}
          onCancel={() => setPending(null)}
        />
      )}
      {pending?.kind === "merge" && (
        <ConfirmDialog
          title="Merge speakers?"
          body={
            <>
              <strong>{pending.source.display_name}</strong> will be merged into{" "}
              <strong>{pending.target.display_name}</strong>. Transcripts that mention the first will be re-labelled.
            </>
          }
          confirmLabel="Merge"
          onConfirm={confirm}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
}

const SpeakerRow = memo(function SpeakerRow({
  speaker,
  others,
  playing,
  onPlay,
  onForget,
  onMerge,
  onRenamed,
}: {
  speaker: Speaker;
  others: Speaker[];
  playing: boolean;
  onPlay: (on: boolean) => void;
  onForget: () => void;
  onMerge: (target: Speaker) => void;
  onRenamed: () => void;
}) {
  const dispatch = useDispatch();
  const [name, setName] = useState(speaker.name ?? "");
  const [saving, setSaving] = useState(false);
  const [mergeTarget, setMergeTarget] = useState("");
  const audioRef = useRef<HTMLAudioElement>(null);

  useEffect(() => setName(speaker.name ?? ""), [speaker.name]);

  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    if (playing) {
      a.currentTime = 0;
      a.play().catch(() => {
        onPlay(false);
        dispatch({ type: "toast", level: "error", message: `Could not play sample for ${speaker.display_name}` });
      });
    } else {
      a.pause();
    }
  }, [playing]);

  const dirty = name.trim() !== (speaker.name ?? "") && name.trim().length > 0;
  const save = async () => {
    if (!dirty) return;
    setSaving(true);
    try {
      await api.nameSpeaker(speaker.id, name.trim());
      onRenamed();
    } catch {
      /* toast */
    } finally {
      setSaving(false);
    }
  };

  return (
    <li className={`speaker-row${speaker.name ? "" : " speaker-unnamed"}`} data-testid="speaker-row" data-speaker-id={speaker.id}>
      <div className="speaker-top">
        <button
          className="icon-btn play-btn"
          disabled={!speaker.has_sample}
          onClick={() => onPlay(!playing)}
          aria-label={playing ? "Stop sample" : `Play sample of ${speaker.display_name}`}
          title={speaker.has_sample ? `${speaker.sample_seconds.toFixed(1)} s sample` : "No sample saved"}
          data-testid="speaker-play"
        >
          {playing ? "■" : "▶"}
        </button>
        <div className="speaker-id">
          <div className="speaker-name">{speaker.display_name}</div>
          <div className="muted small">
            {speaker.has_sample ? `${speaker.sample_seconds.toFixed(1)} s sample · ` : ""}
            first heard {fmtDateTime(speaker.created_at)}
          </div>
        </div>
        {speaker.has_sample && (
          <audio
            ref={audioRef}
            preload="none"
            src={api.speakerSampleUrl(speaker.id)}
            onEnded={() => onPlay(false)}
          />
        )}
      </div>
      <form
        className="speaker-name-form"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <input
          className="input"
          value={name}
          placeholder="Name this speaker"
          aria-label={`Name for ${speaker.display_name}`}
          onChange={(e) => setName(e.target.value)}
          data-testid="speaker-name-input"
        />
        <button className="btn btn-sm btn-primary" type="submit" disabled={!dirty || saving} data-testid="speaker-save">
          {saving ? "Saving…" : "Save"}
        </button>
      </form>
      <div className="speaker-actions">
        <select
          className="select-inline"
          value={mergeTarget}
          onChange={(e) => setMergeTarget(e.target.value)}
          aria-label="Merge into"
        >
          <option value="">Merge into…</option>
          {others
            .filter((o) => o.id !== speaker.id)
            .map((o) => (
              <option key={o.id} value={o.id}>
                {o.display_name}
              </option>
            ))}
        </select>
        <button
          className="btn btn-sm btn-ghost"
          disabled={!mergeTarget}
          onClick={() => {
            const target = others.find((o) => o.id === mergeTarget);
            if (target) onMerge(target);
            setMergeTarget("");
          }}
        >
          Merge
        </button>
        <span className="spacer" />
        <button className="btn btn-sm btn-ghost btn-danger-text" onClick={onForget} data-testid="speaker-forget">
          Forget
        </button>
      </div>
    </li>
  );
});
