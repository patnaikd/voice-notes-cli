import type { Meeting, Segment } from "../types";
import { dayKey, dayLabel } from "./format";

export interface DayGroup {
  key: string;
  label: string;
  meetings: Meeting[];
}

/** Synthesize a meeting for a segment whose meeting has not been loaded (yet). */
function placeholderMeeting(seg: Segment): Meeting {
  return {
    id: seg.meeting_id,
    title: "Loading…",
    segment_ids: [seg.id],
    started_at: seg.started_at,
    ended_at: seg.ended_at,
    notes_status: "none",
    notes_error: null,
    merged_by: null,
    auto_merge_reason: null,
  };
}

export function groupMeetings(meetings: Record<string, Meeting>, segments: Record<string, Segment>): DayGroup[] {
  const all: Record<string, Meeting> = { ...meetings };
  // Segments already listed by a known meeting are covered even if their own
  // meeting_id is momentarily stale (e.g. meeting_removed arrived before the segment upsert).
  const covered = new Set<string>();
  for (const m of Object.values(meetings)) for (const id of m.segment_ids) covered.add(id);
  for (const seg of Object.values(segments)) {
    if (seg.meeting_id && !all[seg.meeting_id] && !covered.has(seg.id)) all[seg.meeting_id] = placeholderMeeting(seg);
  }
  const sorted = Object.values(all).sort((a, b) => (a.started_at < b.started_at ? 1 : a.started_at > b.started_at ? -1 : 0));
  const groups: DayGroup[] = [];
  const now = new Date();
  for (const m of sorted) {
    const key = dayKey(m.started_at);
    let g = groups[groups.length - 1];
    if (!g || g.key !== key) {
      g = { key, label: dayLabel(m.started_at, now), meetings: [] };
      groups.push(g);
    }
    g.meetings.push(m);
  }
  return groups;
}
