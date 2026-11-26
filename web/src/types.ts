// Mirrors docs/web-design.md "Data model" and "WebSocket" sections.

export type SegmentStatus = "recording" | "closing" | "closed" | "empty" | "failed";

export interface Continuation {
  status: "pending" | "done" | "skipped" | "failed";
  is_continuation: boolean | null;
  confidence: number | null;
  reason: string | null;
  previous_segment_id: string | null;
}

export interface Segment {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: SegmentStatus;
  line_count: number;
  meeting_id: string;
  engine: string;
  continuation: Continuation | null;
  error: string | null;
}

export type NotesStatus = "none" | "queued" | "running" | "done" | "failed" | "skipped";

export interface Meeting {
  id: string;
  title: string;
  segment_ids: string[];
  started_at: string;
  ended_at: string | null;
  notes_status: NotesStatus;
  notes_error: string | null;
  merged_by: null | "user" | "auto";
  auto_merge_reason: string | null;
}

export type Source = "mic" | "system";

export interface Line {
  segment_id: string;
  id: string;
  source: Source;
  start: number;
  end: number;
  wall: string;
  text: string;
  speaker: string;
  speaker_uuid: string | null;
}

export interface Speaker {
  id: string;
  name: string | null;
  display_name: string;
  sample_seconds: number;
  created_at: string;
  has_sample: boolean;
}

export type RecorderState = "idle" | "starting" | "recording" | "stopping" | "restarting" | "error";

export interface Recorder {
  state: RecorderState;
  segment_id: string | null;
  segment_started_at: string | null;
  next_boundary_at: string | null;
  engine: string;
  source: string;
  mic: string | null;
  helper_run: number;
  helper_restarts: number;
  message: string | null;
}

export interface Health {
  uptime_s: number;
  server_rss_mb: number;
  helper_rss_mb: number | null;
  helper_pid: number | null;
  event_loop_lag_ms: number;
  ws_clients: number;
  jobs_pending: number;
  jobs_running: number;
  errors_last_hour: number;
  last_error: string | null;
  disk_free_gb: number;
  warnings: string[];
}

export type Engine = "apple" | "fluid";
export type SourceSetting = "both" | "mic" | "system";

export interface Settings {
  engine: Engine;
  source: SourceSetting;
  mic: string | null;
  model: string;
  segment_minutes: number;
  auto_merge: boolean;
  auto_merge_min_confidence: number;
}

export interface Job {
  id: string;
  kind: "notes" | "continuation";
  target: string;
  status: "queued" | "running" | "done" | "failed";
  attempts: number;
  error: string | null;
  created_at: string;
}

export interface Activity {
  ts: string;
  level: "info" | "warning" | "error";
  message: string;
}

export interface LevelPair {
  rms: number;
  peak: number;
}

export interface Levels {
  mic?: LevelPair;
  system?: LevelPair;
}

export interface Snapshot {
  recorder: Recorder;
  settings: Settings;
  segments: Segment[];
  meetings: Meeting[];
  speakers: Speaker[];
  health: Health;
  jobs: Job[];
  live: { segment_id: string | null; lines: Line[] };
  activity: Activity[];
}

export interface Device {
  name: string;
  uid: string;
  default: boolean;
}

export interface Devices {
  microphones: Device[];
  outputs: Device[];
}

export interface SegmentDetail {
  segment: Segment;
  lines: Line[];
  transcript_md: string;
}

export interface MeetingDetail {
  meeting: Meeting;
  segments: Segment[];
  transcript_md: string;
  notes_md: string | null;
}

export type ServerMessage =
  | ({ type: "snapshot" } & Snapshot)
  | { type: "recorder"; recorder: Recorder }
  | { type: "levels"; levels: Levels }
  | { type: "partial"; source: Source; text: string; speaker: string | null }
  | { type: "line"; line: Line }
  | { type: "segment"; segment: Segment }
  | { type: "meeting"; meeting: Meeting }
  | { type: "meeting_removed"; id: string }
  | { type: "speakers"; speakers: Speaker[] }
  | { type: "job"; job: Job }
  | { type: "health"; health: Health }
  | { type: "activity"; activity: Activity }
  | { type: "settings"; settings: Settings }
  | { type: "ping"; t: number };
