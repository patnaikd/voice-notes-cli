import type { Line, Meeting, Segment, Snapshot } from "../types";

export function seg(id: string, over: Partial<Segment> = {}): Segment {
  return {
    id,
    started_at: "2026-11-25T14:00:00-08:00",
    ended_at: "2026-11-25T14:30:00-08:00",
    status: "closed",
    line_count: 10,
    meeting_id: "m1",
    engine: "fluid",
    continuation: null,
    error: null,
    ...over,
  };
}

export function meeting(id: string, over: Partial<Meeting> = {}): Meeting {
  return {
    id,
    title: `Meeting ${id}`,
    segment_ids: [],
    started_at: "2026-11-25T14:00:00-08:00",
    ended_at: "2026-11-25T14:30:00-08:00",
    notes_status: "done",
    notes_error: null,
    merged_by: null,
    auto_merge_reason: null,
    ...over,
  };
}

export function line(id: string, over: Partial<Line> = {}): Line {
  return {
    segment_id: "s1",
    id,
    source: "mic",
    start: 1,
    end: 2,
    wall: "2026-11-25T14:00:01-08:00",
    text: `text ${id}`,
    speaker: "Microphone",
    speaker_uuid: null,
    ...over,
  };
}

export function snapshot(over: Partial<Snapshot> = {}): Snapshot {
  return {
    recorder: {
      state: "idle",
      segment_id: null,
      segment_started_at: null,
      next_boundary_at: null,
      engine: "fluid",
      source: "both",
      mic: null,
      helper_run: 0,
      helper_restarts: 0,
      message: null,
    },
    settings: {
      engine: "fluid",
      source: "both",
      mic: null,
      locale: "en-US",
      model: "claude-haiku-5-5",
      segment_minutes: 30,
      auto_merge: true,
      auto_merge_min_confidence: 0.6,
    },
    segments: [],
    meetings: [],
    speakers: [],
    health: {
      uptime_s: 100,
      server_rss_mb: 120,
      helper_rss_mb: null,
      helper_pid: null,
      event_loop_lag_ms: 1,
      ws_clients: 1,
      jobs_pending: 0,
      jobs_running: 0,
      errors_last_hour: 0,
      last_error: null,
      disk_free_gb: 100,
      warnings: [],
    },
    jobs: [],
    live: { segment_id: null, lines: [] },
    activity: [],
    ...over,
  };
}
