import type {
  Devices,
  Health,
  Job,
  Meeting,
  MeetingDetail,
  Recorder,
  Segment,
  SegmentDetail,
  Settings,
  Snapshot,
  Speaker,
} from "../types";

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type ErrorHandler = (message: string) => void;
let errorHandler: ErrorHandler | null = null;

/** The app registers a handler that turns API failures into toasts. */
export function setApiErrorHandler(handler: ErrorHandler | null): void {
  errorHandler = handler;
}

async function request<T>(method: string, path: string, body?: unknown, opts: { quiet?: boolean } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    const message = `Cannot reach server (${method} ${path})`;
    if (!opts.quiet) errorHandler?.(message);
    throw new ApiError(message, 0);
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`.trim();
    try {
      const data = await res.json();
      if (data && typeof data.detail === "string") detail = data.detail;
      else if (data && data.detail) detail = JSON.stringify(data.detail);
    } catch {
      /* non-JSON error body */
    }
    if (!opts.quiet) errorHandler?.(detail);
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const enc = encodeURIComponent;

export const api = {
  snapshot: () => request<Snapshot>("GET", "/api/snapshot"),
  start: (body: { engine?: string; source?: string; mic?: string | null } = {}) =>
    request<Recorder>("POST", "/api/recording/start", body),
  stop: () => request<Recorder>("POST", "/api/recording/stop"),
  split: () => request<Recorder>("POST", "/api/recording/split"),
  devices: () => request<Devices>("GET", "/api/devices", undefined, { quiet: true }),
  getSettings: () => request<Settings>("GET", "/api/settings"),
  putSettings: (settings: Settings) => request<Settings>("PUT", "/api/settings", settings),
  segmentsBefore: (before: string, limit = 50) =>
    request<{ segments: Segment[]; meetings: Meeting[] }>("GET", `/api/segments?before=${enc(before)}&limit=${limit}`),
  segment: (id: string) => request<SegmentDetail>("GET", `/api/segments/${enc(id)}`),
  meeting: (id: string, opts: { quiet?: boolean } = {}) =>
    request<MeetingDetail>("GET", `/api/meetings/${enc(id)}`, undefined, opts),
  renameMeeting: (id: string, title: string) => request<Meeting>("PATCH", `/api/meetings/${enc(id)}`, { title }),
  mergeMeetings: (meeting_ids: string[]) => request<Meeting>("POST", "/api/meetings/merge", { meeting_ids }),
  splitMeeting: (id: string) => request<Meeting[]>("POST", `/api/meetings/${enc(id)}/split`),
  deleteMeeting: (id: string) => request<{ deleted: string; trash: string }>("DELETE", `/api/meetings/${enc(id)}`),
  deleteLines: (id: string, lines: { segment_id: string; source: string; id: string }[]) =>
    request<{ meeting: Meeting; segments: Segment[] }>("POST", `/api/meetings/${enc(id)}/lines/delete`, { lines }),
  refineMeeting: (id: string) => request<Job>("POST", `/api/meetings/${enc(id)}/refine`),
  speakers: () => request<Speaker[]>("GET", "/api/speakers"),
  nameSpeaker: (id: string, name: string) => request<unknown>("POST", `/api/speakers/${enc(id)}/name`, { name }),
  mergeSpeakers: (source: string, target: string) =>
    request<unknown>("POST", "/api/speakers/merge", { source, target }),
  forgetSpeaker: (id: string) => request<unknown>("DELETE", `/api/speakers/${enc(id)}`),
  exportDayUrl: (day: string) => `/api/export/day/${enc(day)}`,
  speakerSampleUrl: (id: string) => `/api/speakers/${enc(id)}/sample.wav`,
  health: () => request<Health>("GET", "/api/health"),
  jobs: () => request<Job[]>("GET", "/api/jobs"),
};
