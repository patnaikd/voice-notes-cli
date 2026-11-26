import type {
  Activity,
  Health,
  Job,
  Line,
  Meeting,
  Recorder,
  Segment,
  ServerMessage,
  Settings,
  Snapshot,
  Source,
  Speaker,
} from "../types";

export const LIVE_LINES_CAP = 300;
export const ACTIVITY_CAP = 100;
export const JOBS_CAP = 60;
export const SEGMENTS_CAP = 1500;
export const TOASTS_CAP = 5;
/** The server's snapshot holds at most this many segments; fewer means there is nothing older. */
export const SNAPSHOT_SEGMENTS = 200;

export type ConnStatus = "connecting" | "live" | "reconnecting";

export interface Partial {
  text: string;
  speaker: string | null;
}

export interface Toast {
  id: number;
  level: "error" | "info";
  message: string;
}

export interface AppState {
  conn: ConnStatus;
  hasSnapshot: boolean;
  recorder: Recorder | null;
  settings: Settings | null;
  health: Health | null;
  /** Segments keyed by id. */
  segments: Record<string, Segment>;
  /** Meetings keyed by id. */
  meetings: Record<string, Meeting>;
  speakers: Speaker[];
  /** Newest last. */
  jobs: Job[];
  live: { segment_id: string | null; lines: Line[] };
  partials: Record<Source, Partial | null>;
  /** Newest last. */
  activity: Activity[];
  /** True once GET /api/segments?before= returned less than a full page. */
  olderExhausted: boolean;
  toasts: Toast[];
}

export type Action =
  | ServerMessage
  | { type: "conn"; status: ConnStatus }
  | { type: "segments_loaded"; segments: Segment[]; exhausted: boolean }
  | { type: "meetings_loaded"; meetings: Meeting[] }
  | { type: "meetings_replaced"; removed: string[]; meetings: Meeting[] }
  | { type: "toast"; level: Toast["level"]; message: string }
  | { type: "toast_dismiss"; id: number };

export const initialState: AppState = {
  conn: "connecting",
  hasSnapshot: false,
  recorder: null,
  settings: null,
  health: null,
  segments: {},
  meetings: {},
  speakers: [],
  jobs: [],
  live: { segment_id: null, lines: [] },
  partials: { mic: null, system: null },
  activity: [],
  olderExhausted: false,
  toasts: [],
};

let toastSeq = 0;

function byId<T extends { id: string }>(items: T[]): Record<string, T> {
  const out: Record<string, T> = {};
  for (const item of items) out[item.id] = item;
  return out;
}

function capTail<T>(items: T[], cap: number): T[] {
  return items.length > cap ? items.slice(items.length - cap) : items;
}

/** Keep the newest SEGMENTS_CAP segments (by start time) so a page left open for weeks stays bounded. */
function capSegments(segments: Record<string, Segment>): Record<string, Segment> {
  const ids = Object.keys(segments);
  if (ids.length <= SEGMENTS_CAP) return segments;
  const sorted = ids
    .map((id) => segments[id])
    .sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
    .slice(0, SEGMENTS_CAP);
  return byId(sorted);
}

function upsertLine(lines: Line[], line: Line): Line[] {
  // Replacements (speaker revisions) are almost always near the tail.
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].id === line.id) {
      const next = lines.slice();
      next[i] = line;
      return next;
    }
  }
  return capTail([...lines, line], LIVE_LINES_CAP);
}

function upsertJob(jobs: Job[], job: Job): Job[] {
  const idx = jobs.findIndex((j) => j.id === job.id);
  if (idx >= 0) {
    const next = jobs.slice();
    next[idx] = job;
    return next;
  }
  const next = [...jobs, job];
  if (next.length <= JOBS_CAP) return next;
  // Drop the oldest finished jobs first; active jobs are kept.
  const excess = next.length - JOBS_CAP;
  let dropped = 0;
  return next.filter((j) => {
    if (dropped < excess && (j.status === "done" || j.status === "failed")) {
      dropped++;
      return false;
    }
    return true;
  });
}

function fromSnapshot(state: AppState, snap: Snapshot): AppState {
  return {
    ...state,
    hasSnapshot: true,
    recorder: snap.recorder,
    settings: snap.settings,
    health: snap.health,
    segments: byId(snap.segments),
    meetings: byId(snap.meetings),
    speakers: snap.speakers,
    jobs: capTail(snap.jobs, JOBS_CAP),
    live: {
      segment_id: snap.live.segment_id,
      lines: capTail(snap.live.lines, LIVE_LINES_CAP),
    },
    partials: { mic: null, system: null },
    activity: capTail(snap.activity, ACTIVITY_CAP),
    olderExhausted: snap.segments.length < SNAPSHOT_SEGMENTS,
  };
}

export function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "snapshot":
      return fromSnapshot(state, action);
    case "recorder": {
      const recorder = action.recorder;
      let { live, partials } = state;
      if (recorder.segment_id && recorder.segment_id !== live.segment_id) {
        live = { ...live, segment_id: recorder.segment_id };
      }
      if (recorder.state === "idle" || recorder.state === "error") {
        partials = { mic: null, system: null };
      }
      return { ...state, recorder, live, partials };
    }
    case "partial": {
      const value = action.text ? { text: action.text, speaker: action.speaker } : null;
      const prev = state.partials[action.source];
      if (prev === value || (prev && value && prev.text === value.text && prev.speaker === value.speaker)) {
        return state;
      }
      return { ...state, partials: { ...state.partials, [action.source]: value } };
    }
    case "line": {
      const line = action.line;
      // A finalized line supersedes the partial for its source.
      const partials =
        state.partials[line.source] !== null ? { ...state.partials, [line.source]: null } : state.partials;
      return {
        ...state,
        partials,
        live: { segment_id: state.live.segment_id ?? line.segment_id, lines: upsertLine(state.live.lines, line) },
      };
    }
    case "segment":
      return { ...state, segments: capSegments({ ...state.segments, [action.segment.id]: action.segment }) };
    case "meeting":
      return { ...state, meetings: { ...state.meetings, [action.meeting.id]: action.meeting } };
    case "meeting_removed": {
      if (!(action.id in state.meetings)) return state;
      const meetings = { ...state.meetings };
      delete meetings[action.id];
      return { ...state, meetings };
    }
    case "speakers":
      return { ...state, speakers: action.speakers };
    case "job":
      return { ...state, jobs: upsertJob(state.jobs, action.job) };
    case "health":
      return { ...state, health: action.health };
    case "activity":
      return { ...state, activity: capTail([...state.activity, action.activity], ACTIVITY_CAP) };
    case "settings":
      return { ...state, settings: action.settings };
    case "levels":
    case "ping":
      // Levels never enter React state (see levelStore); pings are keep-alives.
      return state;
    case "conn":
      return state.conn === action.status ? state : { ...state, conn: action.status };
    case "segments_loaded": {
      const segments = { ...state.segments };
      for (const s of action.segments) segments[s.id] = s;
      return { ...state, segments: capSegments(segments), olderExhausted: action.exhausted };
    }
    case "meetings_loaded": {
      const meetings = { ...state.meetings };
      for (const m of action.meetings) meetings[m.id] = m;
      return { ...state, meetings };
    }
    case "meetings_replaced": {
      const meetings = { ...state.meetings };
      for (const id of action.removed) delete meetings[id];
      for (const m of action.meetings) meetings[m.id] = m;
      return { ...state, meetings };
    }
    case "toast": {
      const toast: Toast = { id: ++toastSeq, level: action.level, message: action.message };
      return { ...state, toasts: capTail([...state.toasts, toast], TOASTS_CAP) };
    }
    case "toast_dismiss":
      return { ...state, toasts: state.toasts.filter((t) => t.id !== action.id) };
    default:
      return state;
  }
}
