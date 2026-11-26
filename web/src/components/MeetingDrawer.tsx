import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api/client";
import { fmtDate, fmtRange, fmtTimeSec, speakerHue } from "../lib/format";
import { useAppState, useDispatch } from "../state/store";
import type { Line, Meeting, MeetingDetail, Segment } from "../types";
import { ConfirmDialog } from "./ConfirmDialog";
import { DeleteMeetingButton } from "./DeleteMeetingButton";

type Tab = "notes" | "transcript";
type SegmentLines = { segment: Segment; lines: Line[] };

const OPEN = new Set(["recording", "closing"]);
const lineKey = (l: Line) => `${l.segment_id}|${l.source}|${l.id}`;

export function MeetingDrawer({ meetingId, onClose }: { meetingId: string; onClose: () => void }) {
  const meeting = useAppState((s) => s.meetings[meetingId] ?? null);
  const segments = useAppState((s) => s.segments);
  const dispatch = useDispatch();
  const [tab, setTab] = useState<Tab>("notes");
  const [detail, setDetail] = useState<MeetingDetail | null>(null);
  const [transcript, setTranscript] = useState<SegmentLines[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refining, setRefining] = useState(false);

  // Refetch when notes, membership or line counts change (e.g. notes job finished, lines deleted).
  const version = meeting
    ? [
        meeting.notes_status,
        meeting.notes_stale ? "stale" : "",
        ...meeting.segment_ids.map((id) => `${id}:${segments[id]?.line_count ?? "?"}:${segments[id]?.status ?? ""}`),
      ].join("|")
    : "gone";

  const loaded = useRef(false);
  useEffect(() => {
    // Once loaded, a meeting that leaves the store was merged away or deleted: nothing left to fetch.
    if (version === "gone" && loaded.current) {
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    api
      .meeting(meetingId, { quiet: true })
      .then(async (d) => {
        if (!alive) return;
        loaded.current = true;
        setDetail(d);
        setError(null);
        if (tab === "transcript") {
          const parts = await Promise.all(d.segments.map((s) => api.segment(s.id)));
          if (alive) setTranscript(parts.map((p) => ({ segment: p.segment, lines: p.lines })));
        }
      })
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [meetingId, version, tab]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !document.querySelector(".modal") && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const m = meeting ?? detail?.meeting ?? null;
  const isLive = !!m && m.segment_ids.some((id) => OPEN.has(segments[id]?.status ?? ""));
  const notesBusy = m?.notes_status === "queued" || m?.notes_status === "running";

  const refine = async () => {
    if (!m) return;
    setRefining(true);
    try {
      const job = await api.refineMeeting(m.id);
      dispatch({ type: "job", job });
      setTab("notes");
    } catch {
      /* toast */
    } finally {
      setRefining(false);
    }
  };

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={m?.title ?? "Meeting"}
        data-testid="meeting-drawer"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="drawer-head">
          <div>
            <h2>{m?.title ?? "Meeting"}</h2>
            {m && (
              <div className="muted small">
                {fmtDate(m.started_at)} · {fmtRange(m.started_at, m.ended_at)} · {m.segment_ids.length}{" "}
                {m.segment_ids.length === 1 ? "segment" : "segments"}
                {m.merged_by === "auto" && m.auto_merge_reason ? ` · auto-merged: ${m.auto_merge_reason}` : ""}
              </div>
            )}
          </div>
          <div className="drawer-actions">
            {meeting && (
              <>
                <button
                  className="btn btn-sm"
                  data-testid="drawer-refine-btn"
                  onClick={refine}
                  disabled={refining || isLive || notesBusy}
                  title={isLive ? "Notes are written when recording of this meeting ends" : "Write the notes again from the current transcript"}
                >
                  {refining ? "Queuing…" : notesBusy ? "Writing notes…" : "Regenerate notes"}
                </button>
                <DeleteMeetingButton meeting={meeting} disabled={isLive} onDeleted={onClose} />
              </>
            )}
            <button className="icon-btn" aria-label="Close" onClick={onClose}>
              ×
            </button>
          </div>
        </header>
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "notes"} className="tab" onClick={() => setTab("notes")}>
            Notes
          </button>
          <button
            role="tab"
            aria-selected={tab === "transcript"}
            className="tab"
            data-testid="tab-transcript"
            onClick={() => setTab("transcript")}
          >
            Transcript
          </button>
          {loading && <span className="muted small tab-status">Loading…</span>}
        </div>
        <div className="drawer-body">
          {!meeting && !loading && <div className="empty">This meeting no longer exists (it may have been merged or deleted).</div>}
          {error && <div className="inline-error">Could not load meeting: {error}</div>}
          {detail && tab === "notes" && (
            <>
              {m?.notes_stale && (
                <div className="stale-banner" data-testid="stale-banner">
                  <span>The transcript changed after these notes were written.</span>
                  <button className="btn btn-sm btn-primary" onClick={refine} disabled={refining || notesBusy}>
                    Regenerate notes
                  </button>
                </div>
              )}
              <div className="markdown" data-testid="notes-md">
                {detail.notes_md ? (
                  <Markdown remarkPlugins={[remarkGfm]}>{detail.notes_md}</Markdown>
                ) : (
                  <div className="empty">
                    {m?.notes_status === "failed"
                      ? `Notes failed: ${m.notes_error ?? "unknown error"}`
                      : notesBusy
                        ? "Notes are being written…"
                        : m?.notes_status === "skipped"
                          ? "No speech in this meeting, so no notes."
                          : "No notes yet."}
                  </div>
                )}
              </div>
            </>
          )}
          {tab === "transcript" && m && transcript && (
            <TranscriptEditor meeting={m} parts={transcript} onChanged={setTranscript} />
          )}
        </div>
      </aside>
    </div>
  );
}

function TranscriptEditor({
  meeting,
  parts,
  onChanged,
}: {
  meeting: Meeting;
  parts: SegmentLines[];
  onChanged: (parts: SegmentLines[]) => void;
}) {
  const dispatch = useDispatch();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<Line[] | null>(null);
  const [busy, setBusy] = useState(false);

  const byKey = useMemo(() => {
    const map = new Map<string, Line>();
    for (const part of parts) for (const line of part.lines) map.set(lineKey(line), line);
    return map;
  }, [parts]);
  // Drop selections whose lines disappeared after a refetch.
  useEffect(() => {
    setSelected((current) => {
      const kept = new Set([...current].filter((key) => byKey.has(key)));
      return kept.size === current.size ? current : kept;
    });
  }, [byKey]);

  const toggle = useCallback((key: string, on: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  const remove = async () => {
    const lines = pending ?? [];
    setPending(null);
    if (!lines.length) return;
    setBusy(true);
    try {
      const result = await api.deleteLines(
        meeting.id,
        lines.map((l) => ({ segment_id: l.segment_id, source: l.source, id: l.id })),
      );
      dispatch({ type: "meeting", meeting: result.meeting });
      for (const segment of result.segments) dispatch({ type: "segment", segment });
      const gone = new Set(lines.map(lineKey));
      onChanged(parts.map((p) => ({ ...p, lines: p.lines.filter((l) => !gone.has(lineKey(l))) })));
      setSelected(new Set());
      dispatch({
        type: "toast",
        level: "info",
        message: `Deleted ${lines.length} ${lines.length === 1 ? "line" : "lines"}. Regenerate notes to update the summary.`,
      });
    } catch {
      /* toast */
    } finally {
      setBusy(false);
    }
  };

  const total = parts.reduce((n, p) => n + p.lines.length, 0);
  if (total === 0) return <div className="empty">Empty transcript.</div>;

  return (
    <div className="transcript-editor" data-testid="transcript-editor">
      <div className="transcript-toolbar">
        <span className="muted small">
          {selected.size ? `${selected.size} selected` : `${total} lines · select lines to delete them`}
        </span>
        {selected.size > 0 && (
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set())}>
              Clear
            </button>
            <button
              className="btn btn-sm btn-danger"
              data-testid="delete-lines-btn"
              disabled={busy}
              onClick={() => setPending([...selected].map((key) => byKey.get(key)!).filter(Boolean))}
            >
              {busy ? "Deleting…" : `Delete ${selected.size}`}
            </button>
          </>
        )}
      </div>
      {parts.map(({ segment, lines }) => {
        const live = OPEN.has(segment.status);
        return (
          <section key={segment.id} className="transcript-segment">
            <div className="seg-divider">
              {fmtRange(segment.started_at, segment.ended_at)}
              {live ? " · recording" : ""}
            </div>
            {lines.length === 0 ? (
              <div className="muted small transcript-empty">No speech.</div>
            ) : (
              <ul className="lines">
                {lines.map((line) => {
                  const key = lineKey(line);
                  return (
                    <li
                      key={key}
                      className={`line edit-line${selected.has(key) ? " edit-line-selected" : ""}`}
                      data-testid="transcript-line"
                    >
                      <input
                        type="checkbox"
                        aria-label={`Select line: ${line.text}`}
                        checked={selected.has(key)}
                        disabled={live || busy}
                        onChange={(e) => toggle(key, e.target.checked)}
                      />
                      <span className="line-time tabular">{fmtTimeSec(line.wall)}</span>
                      <span className="speaker" style={{ ["--hue" as string]: speakerHue(line.speaker) }}>
                        {line.speaker}
                      </span>
                      <span className="line-text">{line.text}</span>
                      <button
                        className="icon-btn line-delete"
                        aria-label="Delete line"
                        title={live ? "Wait until this segment finishes recording" : "Delete this line"}
                        disabled={live || busy}
                        onClick={() => setPending([line])}
                      >
                        ×
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        );
      })}
      {pending && (
        <ConfirmDialog
          title={pending.length === 1 ? "Delete this line?" : `Delete ${pending.length} lines?`}
          body={
            <>
              {pending.length === 1 ? <>“{pending[0].text}”</> : "The selected lines"} will be removed from the transcript.
              Existing notes are kept until you regenerate them.
            </>
          }
          confirmLabel={pending.length === 1 ? "Delete line" : `Delete ${pending.length} lines`}
          danger
          onConfirm={remove}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
}
