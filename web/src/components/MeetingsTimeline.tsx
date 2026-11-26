import { memo, useCallback, useMemo, useState } from "react";
import { api } from "../api/client";
import { groupMeetings } from "../lib/grouping";
import { useAppState, useDispatch, useStore } from "../state/store";
import type { Meeting } from "../types";
import { MeetingCard } from "./MeetingCard";

export function MeetingsTimeline({ onOpen }: { onOpen: (id: string) => void }) {
  const meetings = useAppState((s) => s.meetings);
  const segments = useAppState((s) => s.segments);
  const exhausted = useAppState((s) => s.olderExhausted);
  const hasSnapshot = useAppState((s) => s.hasSnapshot);
  const store = useStore();
  const dispatch = useDispatch();
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);

  const groups = useMemo(() => groupMeetings(meetings, segments), [meetings, segments]);

  // Drop selections for meetings that no longer exist (merged away, removed).
  const liveSelected = selected.filter((id) => id in meetings);

  const toggle = useCallback((id: string, on: boolean) => {
    setSelected((prev) => (on ? (prev.includes(id) ? prev : [...prev, id]) : prev.filter((x) => x !== id)));
  }, []);

  const merge = async () => {
    if (liveSelected.length < 2) return;
    setBusy(true);
    try {
      const merged = await api.mergeMeetings(liveSelected);
      dispatch({ type: "meetings_replaced", removed: liveSelected.filter((id) => id !== merged.id), meetings: [merged] });
      dispatch({ type: "toast", level: "info", message: `Merged ${liveSelected.length} meetings into “${merged.title}”` });
      setSelected([]);
    } catch {
      /* toast already shown */
    } finally {
      setBusy(false);
    }
  };

  const loadOlder = async () => {
    const segs = Object.values(store.getState().segments);
    if (segs.length === 0) return;
    const oldest = segs.reduce((a, b) => (a.started_at <= b.started_at ? a : b));
    setLoadingOlder(true);
    try {
      // The server returns the older page together with the meetings it references.
      const page = await api.segmentsBefore(oldest.id, 50);
      dispatch({ type: "segments_loaded", segments: page.segments, exhausted: page.segments.length < 50 });
      if (page.meetings.length) dispatch({ type: "meetings_loaded", meetings: page.meetings });
    } catch {
      /* toast already shown */
    } finally {
      setLoadingOlder(false);
    }
  };

  const total = groups.reduce((n, g) => n + g.meetings.length, 0);

  return (
    <div className="panel meetings-panel">
      <div className="panel-head">
        <h2>Meetings</h2>
        <div className="panel-actions">
          {liveSelected.length > 0 && (
            <button className="btn btn-ghost btn-sm" onClick={() => setSelected([])}>
              Clear ({liveSelected.length})
            </button>
          )}
          <button
            className="btn btn-sm"
            data-testid="merge-btn"
            disabled={liveSelected.length < 2 || busy}
            onClick={merge}
            title={liveSelected.length < 2 ? "Select two or more meetings to merge" : "Merge the selected meetings"}
          >
            Merge selected{liveSelected.length >= 2 ? ` (${liveSelected.length})` : ""}
          </button>
        </div>
      </div>
      <div className="meetings-scroll">
        {total === 0 ? (
          <div className="empty">{hasSnapshot ? "No meetings yet. Recorded segments appear here." : "Loading…"}</div>
        ) : (
          groups.map((g) => (
            <section key={g.key} className="day">
              <h3 className="day-head">
                {g.label}
                <span className="muted small"> · {g.meetings.length} {g.meetings.length === 1 ? "meeting" : "meetings"}</span>
              </h3>
              <div className="cards">
                {g.meetings.map((m) => (
                  <MeetingCardConnected
                    key={m.id}
                    meeting={m}
                    selected={liveSelected.includes(m.id)}
                    onToggle={toggle}
                    onOpen={onOpen}
                  />
                ))}
              </div>
            </section>
          ))
        )}
        {total > 0 && (
          <div className="load-older">
            {exhausted ? (
              <span className="muted small">No older segments.</span>
            ) : (
              <button className="btn btn-ghost btn-sm" onClick={loadOlder} disabled={loadingOlder} data-testid="load-older">
                {loadingOlder ? "Loading…" : "Load older"}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** Resolves the meeting's segments from the store so the card re-renders only when they change. */
const MeetingCardConnected = memo(function MeetingCardConnected(props: {
  meeting: Meeting;
  selected: boolean;
  onToggle: (id: string, on: boolean) => void;
  onOpen: (id: string) => void;
}) {
  const segments = useAppState((s) => s.segments);
  const segs = useMemo(
    () => props.meeting.segment_ids.map((id) => segments[id] ?? id),
    [props.meeting.segment_ids, segments],
  );
  return <MeetingCard {...props} segments={segs} />;
});
