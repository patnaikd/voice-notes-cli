import { memo, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { fmtRange, fmtTime } from "../lib/format";
import { useDispatch } from "../state/store";
import type { Meeting, NotesStatus, Segment } from "../types";

type SegOrId = Segment | string;

interface Props {
  meeting: Meeting;
  segments: SegOrId[];
  selected: boolean;
  onToggle: (id: string, on: boolean) => void;
  onOpen: (id: string) => void;
}

const NOTES_LABEL: Record<NotesStatus, string> = {
  none: "No notes",
  queued: "Notes queued",
  running: "Writing notes…",
  done: "Notes ready",
  failed: "Notes failed",
  skipped: "No speech",
};

function sameSegments(a: SegOrId[], b: SegOrId[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

export const MeetingCard = memo(
  function MeetingCard({ meeting, segments, selected, onToggle, onOpen }: Props) {
    const dispatch = useDispatch();
    const [busy, setBusy] = useState<null | "split" | "refine">(null);
    const isLive = segments.some((s) => typeof s !== "string" && (s.status === "recording" || s.status === "closing"));
    const lines = segments.reduce((n, s) => n + (typeof s === "string" ? 0 : s.line_count), 0);
    const multi = meeting.segment_ids.length > 1;
    const placeholder = meeting.title === "Loading…";

    const split = async () => {
      setBusy("split");
      try {
        const parts = await api.splitMeeting(meeting.id);
        dispatch({ type: "meetings_replaced", removed: [meeting.id], meetings: parts });
      } catch {
        /* toast */
      } finally {
        setBusy(null);
      }
    };
    const refine = async () => {
      setBusy("refine");
      try {
        const job = await api.refineMeeting(meeting.id);
        dispatch({ type: "job", job });
        dispatch({ type: "toast", level: "info", message: `Notes re-queued for “${meeting.title}”` });
      } catch {
        /* toast */
      } finally {
        setBusy(null);
      }
    };

    return (
      <article
        className={`card${selected ? " card-selected" : ""}${isLive ? " card-live" : ""}`}
        data-testid="meeting-card"
        data-meeting-id={meeting.id}
      >
        <div className="card-row">
          <input
            type="checkbox"
            className="card-check"
            data-testid="meeting-select"
            checked={selected}
            disabled={placeholder}
            aria-label={`Select ${meeting.title}`}
            onChange={(e) => onToggle(meeting.id, e.target.checked)}
          />
          <div className="card-main">
            <div className="card-title-row">
              <TitleEditor meeting={meeting} disabled={placeholder} />
              {isLive && <span className="badge badge-rec">Live</span>}
              {meeting.merged_by && (
                <span
                  className={`badge badge-merge badge-merge-${meeting.merged_by}`}
                  title={meeting.auto_merge_reason ?? (meeting.merged_by === "user" ? "Merged by you" : "Merged automatically")}
                >
                  {meeting.merged_by === "auto" ? "Auto-merged" : "Merged"}
                </span>
              )}
            </div>
            <div className="card-meta">
              <span className="tabular">{fmtRange(meeting.started_at, meeting.ended_at)}</span>
              <span className="sep">·</span>
              <span>
                {meeting.segment_ids.length} {meeting.segment_ids.length === 1 ? "segment" : "segments"}
              </span>
              <span className="sep">·</span>
              <span>{lines} lines</span>
            </div>
          </div>
          <span
            className={`notes-pill notes-${meeting.notes_status}`}
            data-testid="notes-status"
            data-status={meeting.notes_status}
            title={meeting.notes_error ?? undefined}
          >
            {NOTES_LABEL[meeting.notes_status] ?? meeting.notes_status}
          </span>
        </div>
        <div className="chips">
          {segments.map((s) => (
            <SegmentChip key={typeof s === "string" ? s : s.id} seg={s} />
          ))}
        </div>
        {meeting.notes_status === "failed" && meeting.notes_error && (
          <div className="card-error" title={meeting.notes_error}>
            {meeting.notes_error}
          </div>
        )}
        <div className="card-actions">
          <button className="btn btn-sm" onClick={() => onOpen(meeting.id)} disabled={placeholder}>
            Open
          </button>
          <button
            className="btn btn-ghost btn-sm"
            data-testid="refine-btn"
            onClick={refine}
            disabled={
              busy !== null ||
              placeholder ||
              isLive ||
              meeting.notes_status === "queued" ||
              meeting.notes_status === "running"
            }
            title={isLive ? "Notes are written when recording of this meeting ends" : undefined}
          >
            {busy === "refine" ? "Queuing…" : "Regenerate notes"}
          </button>
          {multi && (
            <button
              className="btn btn-ghost btn-sm"
              data-testid="split-meeting-btn"
              onClick={split}
              disabled={busy !== null}
              title="Split back into one meeting per segment"
            >
              {busy === "split" ? "Splitting…" : "Split"}
            </button>
          )}
        </div>
      </article>
    );
  },
  (a, b) =>
    a.meeting === b.meeting &&
    a.selected === b.selected &&
    a.onToggle === b.onToggle &&
    a.onOpen === b.onOpen &&
    sameSegments(a.segments, b.segments),
);

function TitleEditor({ meeting, disabled }: { meeting: Meeting; disabled: boolean }) {
  const dispatch = useDispatch();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(meeting.title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!editing) setValue(meeting.title);
  }, [meeting.title, editing]);
  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const save = async () => {
    const title = value.trim();
    setEditing(false);
    if (!title || title === meeting.title) {
      setValue(meeting.title);
      return;
    }
    dispatch({ type: "meeting", meeting: { ...meeting, title } });
    try {
      const saved = await api.renameMeeting(meeting.id, title);
      dispatch({ type: "meeting", meeting: saved });
    } catch {
      dispatch({ type: "meeting", meeting });
    }
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="title-input"
        value={value}
        maxLength={200}
        aria-label="Meeting title"
        data-testid="meeting-title-input"
        onChange={(e) => setValue(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") {
            setValue(meeting.title);
            setEditing(false);
          }
        }}
      />
    );
  }
  return (
    <button
      className="card-title"
      title="Click to rename"
      data-testid="meeting-title"
      disabled={disabled}
      onClick={() => setEditing(true)}
    >
      {meeting.title}
    </button>
  );
}

function idToTime(id: string): string {
  const m = /_(\d{2})-(\d{2})/.exec(id);
  return m ? `${m[1]}:${m[2]}` : id;
}

function continuationText(seg: Segment): { icon: string; cls: string; tip: string } | null {
  const c = seg.continuation;
  if (!c) return null;
  const conf = c.confidence !== null ? ` (${Math.round(c.confidence * 100)}%)` : "";
  const reason = c.reason ? `\n${c.reason}` : "";
  switch (c.status) {
    case "pending":
      return { icon: "…", cls: "cont-pending", tip: "Checking whether this continues the previous meeting" };
    case "skipped":
      return null;
    case "failed":
      return { icon: "!", cls: "cont-failed", tip: `Continuation check failed${reason}` };
    case "done":
      return c.is_continuation
        ? { icon: "↩", cls: "cont-yes", tip: `Continues previous meeting${conf}${reason}` }
        : { icon: "∤", cls: "cont-no", tip: `New meeting${conf}${reason}` };
  }
}

function SegmentChip({ seg }: { seg: SegOrId }) {
  if (typeof seg === "string") {
    return (
      <span className="chip chip-unknown" data-testid="segment-chip" title={`Segment ${seg} (not loaded)`}>
        {idToTime(seg)}
      </span>
    );
  }
  const cont = continuationText(seg);
  const tip = [
    `Segment ${seg.id}`,
    `Status: ${seg.status}`,
    `${seg.line_count} lines · ${seg.engine}`,
    seg.error ? `Error: ${seg.error}` : null,
    cont ? cont.tip : null,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span className={`chip chip-${seg.status}`} data-testid="segment-chip" data-status={seg.status} title={tip}>
      <span className="chip-dot" aria-hidden="true" />
      <span className="tabular">
        {fmtTime(seg.started_at)}
        {seg.ended_at ? `–${fmtTime(seg.ended_at)}` : ""}
      </span>
      <span className="chip-count">{seg.line_count}</span>
      {cont && (
        <span className={`chip-cont ${cont.cls}`} aria-label={cont.tip}>
          {cont.icon}
        </span>
      )}
    </span>
  );
}
