import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api/client";
import { fmtDate, fmtRange } from "../lib/format";
import { useAppState } from "../state/store";
import type { MeetingDetail } from "../types";

type Tab = "notes" | "transcript";

export function MeetingDrawer({ meetingId, onClose }: { meetingId: string; onClose: () => void }) {
  const meeting = useAppState((s) => s.meetings[meetingId] ?? null);
  const [tab, setTab] = useState<Tab>("notes");
  const [detail, setDetail] = useState<MeetingDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Refetch when the meeting's notes or membership change (e.g. notes job finished).
  const version = meeting ? `${meeting.notes_status}|${meeting.segment_ids.join(",")}` : "gone";
  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .meeting(meetingId, { quiet: true })
      .then((d) => {
        if (!alive) return;
        setDetail(d);
        setError(null);
      })
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [meetingId, version]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const m = meeting ?? detail?.meeting ?? null;

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
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "notes"} className="tab" onClick={() => setTab("notes")}>
            Notes
          </button>
          <button role="tab" aria-selected={tab === "transcript"} className="tab" onClick={() => setTab("transcript")}>
            Transcript
          </button>
          {loading && <span className="muted small tab-status">Loading…</span>}
        </div>
        <div className="drawer-body">
          {!meeting && !loading && <div className="empty">This meeting no longer exists (it may have been merged).</div>}
          {error && <div className="inline-error">Could not load meeting: {error}</div>}
          {detail && tab === "notes" && (
            <div className="markdown" data-testid="notes-md">
              {detail.notes_md ? (
                <Markdown remarkPlugins={[remarkGfm]}>{detail.notes_md}</Markdown>
              ) : (
                <div className="empty">
                  {m?.notes_status === "failed"
                    ? `Notes failed: ${m.notes_error ?? "unknown error"}`
                    : m?.notes_status === "queued" || m?.notes_status === "running"
                      ? "Notes are being written…"
                      : m?.notes_status === "skipped"
                        ? "No speech in this meeting, so no notes."
                        : "No notes yet."}
                </div>
              )}
            </div>
          )}
          {detail && tab === "transcript" && (
            <div className="markdown transcript" data-testid="transcript-md">
              {detail.transcript_md ? <Markdown remarkPlugins={[remarkGfm]}>{detail.transcript_md}</Markdown> : <div className="empty">Empty transcript.</div>}
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}
