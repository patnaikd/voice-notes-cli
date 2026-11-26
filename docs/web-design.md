# Web interface design and protocol contract

This document is the contract between the Python backend (`src/voice_notes/web/`) and the React frontend (`web/`). Both sides must follow it exactly. If you must change it, update this file in the same change.

## Goals

- Replace the Textual dashboard with a browser UI that updates in real time over a WebSocket. Remove `tui.py`, its tests, the `textual` dependency, and the `--ui` option. Keep plain CLI `record` as is.
- Record all day. The user starts and stops recording manually in the UI (no auto-start on first launch).
- Split recording into **segments** at wall-clock boundaries: every `:00` and `:30` (configurable `segment_minutes`, default 30; boundaries are multiples of `segment_minutes` since local midnight). The first segment runs from Start to the next boundary. Stop ends the current segment early.
- All LLM work runs asynchronously in a job queue and never blocks capture or the UI.
- When a segment closes, an LLM call decides whether it continues the previous segment's meeting. If yes, the segment is merged automatically into that meeting.
- The user can merge meetings, split a merged meeting back into its segments, rename meetings, regenerate notes.
- Merging re-runs the LLM on the combined transcript to produce one notes.md for the meeting. Originals are never deleted.
- Speaker naming from the browser (list, play sample, name, merge, forget).
- Survive running all day: watchdog, memory monitoring, bounded buffers, automatic restart of the capture helper, crash recovery, launchd agent for the whole server.

## Running

```sh
uv run voice-notes serve [--host 127.0.0.1] [--port 8765] [--data-dir meetings] [--open]
uv run voice-notes service install|uninstall|status   # launchd agent with KeepAlive
```

- Bind to `127.0.0.1` by default. There is no authentication, so refuse a non-loopback host unless `--allow-remote` is passed.
- The built frontend lives in `src/voice_notes/web/static/` (built by `npm --prefix web run build`, output dir configured in `vite.config.ts`). FastAPI serves it at `/`. If it is missing, `/` returns a short HTML page saying to run the build.
- Dev mode: `npm --prefix web run dev` (Vite on 5173) proxies `/api` and `/ws` to `127.0.0.1:8765`.
- Backend stack: FastAPI + uvicorn (`uvicorn[standard]` for websockets), `psutil` for memory. Add to `pyproject.toml` dependencies; remove `textual`.
- Hidden test options on `serve`: `--segment-seconds N` (overrides minutes, boundaries are multiples of N seconds since midnight), `--helper-command "<cmd>"` (replace the Swift helper with a fake process that prints the same JSON lines; the usual helper arguments are appended), `--fake-llm` (deterministic local LLM stub; see below), `--grace-seconds N` (late-final grace period), `--stall-seconds N` (stall watchdog). Env equivalents: `VOICE_NOTES_SEGMENT_SECONDS`, `VOICE_NOTES_HELPER_COMMAND`, `VOICE_NOTES_FAKE_LLM=1`, `VOICE_NOTES_GRACE_SECONDS`, `VOICE_NOTES_STALL_SECONDS`.
- Build the frontend before `serve`: `npm --prefix web ci && npm --prefix web run build`. `static/` is not git-ignored, and the wheel includes it (`artifacts` in `pyproject.toml`).

## Capture pipeline (backend)

- One capture helper process runs continuously while recording. **Segments are rolled in Python** at boundaries. Do not restart the helper at each boundary (the fluid engine takes many seconds to load models).
- Each helper run has a run number `r`. Python records the wall-clock time (`time.time()`) at the helper's `ready` event as the run's origin. Helper times (`start`/`end` seconds) map to wall clock as `origin + t`.
- Each finalized `segment` event from the helper is routed to the recording segment whose wall-clock window contains `origin + start`. Its id is rewritten to `f"r{r}-{id}"` so ids stay unique across helper restarts. Times in the segment journal are rewritten to be **relative to that segment's start** (`origin + t - segment_start_wall`, clamped at 0). Later events with the same id (speaker revisions) go to the segment that received the first version (keep a bounded id→segment map, drop entries for closed segments). Revisions of a line whose segment has already closed are dropped (a bounded set of closed ids is remembered), never re-routed into the current segment.
- After a boundary, the previous segment stays open for a **grace period** (default `min(20 s, half a segment)`) to receive late finalizations, then closes. Close = render `transcript.md`, set status, enqueue LLM jobs.
- Boundaries use elapsed seconds since local midnight (resolved with `mktime`, so DST days work) and are capped at the next local midnight. With the default 30 minutes they fall on wall-clock `:00`/`:30` on every day, including DST change days.
- Segment journal format is unchanged from the CLI (`events.jsonl` + `meeting.json` + `transcript.md` + `notes*.md`), so `voice-notes refine` still works on a segment folder. `meeting.json` gets extra keys: `"segment_start"`, `"segment_end"` (ISO 8601 with offset), `"kind": "segment"`.
- Level events: coalesce and broadcast at most 10 per second. Partial events: broadcast at most 5 per second per source (latest wins). Never journal levels or partials.

## On-disk layout (`--data-dir`, default `meetings/`)

```
meetings/
  2026-11-25_14-00-00/        one folder per segment (named by the segment's start, local time)
    meeting.json  events.jsonl  transcript.md  notes.md (single-segment meeting notes)
  merged/<meeting_id>/        only for meetings with 2+ segments
    transcript.md  notes.md  notes-part-*.{md,json}
  index.json                  segments + meetings index (atomic writes, source of truth for grouping)
  jobs.json                   persisted LLM job queue (pending/running jobs are re-queued on start)
  state.json                  {"recording_desired": bool, "settings": {...}}
  logs/server.log             rotating log (5 MB x 5)
```

Existing folders from the CLI (no `kind`) are imported into `index.json` on first start as closed segments, each its own meeting.

## Data model (JSON as sent to the client)

```ts
type SegmentStatus = "recording" | "closing" | "closed" | "empty" | "failed";
interface Segment {
  id: string;                 // folder name, e.g. "2026-11-25_14-00-00"
  started_at: string;         // ISO 8601 with offset
  ended_at: string | null;    // null while recording
  status: SegmentStatus;      // "empty" = closed with no finalized speech
  line_count: number;
  meeting_id: string;
  engine: string;
  continuation: null | {      // result of the LLM continuation check against the previous segment
    status: "pending" | "done" | "skipped" | "failed";
    is_continuation: boolean | null;
    confidence: number | null; // 0..1
    reason: string | null;
    previous_segment_id: string | null;
  };
  error: string | null;
}
type NotesStatus = "none" | "queued" | "running" | "done" | "failed" | "skipped";
interface Meeting {
  id: string;                 // 12 hex chars
  title: string;              // default "Meeting HH:MM" from first segment start; user can rename
  segment_ids: string[];      // ordered by segment start
  started_at: string;
  ended_at: string | null;
  notes_status: NotesStatus;
  notes_error: string | null;
  notes_stale?: boolean;      // transcript lines were deleted after these notes were written
  merged_by: null | "user" | "auto";
  auto_merge_reason: string | null;
}
interface Line {              // finalized transcript line
  segment_id: string;
  id: string;                 // "r{run}-{helper id}"; same id replaces the earlier line
  source: "mic" | "system";
  start: number;              // seconds since segment start
  end: number;
  wall: string;               // ISO wall-clock time of start
  text: string;
  speaker: string;            // display label (registry name, or the 8-character ID "3f2a91c4" for an unnamed voice, "Microphone", "Remote audio")
  speaker_uuid: string | null;
}
interface Speaker { id: string; name: string | null; display_name: string; sample_seconds: number; created_at: string; has_sample: boolean; }
type RecorderState = "idle" | "starting" | "recording" | "stopping" | "restarting" | "error";
interface Recorder {
  state: RecorderState;
  segment_id: string | null;
  segment_started_at: string | null;
  next_boundary_at: string | null;   // ISO; UI shows a countdown
  engine: string; source: string; mic: string | null;
  helper_run: number;
  helper_restarts: number;
  message: string | null;            // human-readable status or last error
}
interface Health {
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
  warnings: string[];                // e.g. "Server memory above 1024 MB", "No audio level for 120 s"
}
interface Settings { engine: "apple" | "fluid"; source: "both" | "mic" | "system"; mic: string | null; model: string; segment_minutes: number; auto_merge: boolean; auto_merge_min_confidence: number; }
interface Job { id: string; kind: "notes" | "continuation"; target: string; status: "queued" | "running" | "done" | "failed"; attempts: number; error: string | null; created_at: string; }
```

Defaults: `engine "fluid"`, `source "both"`, `mic null`, `model "claude-haiku-5-5"`, `segment_minutes 30`, `auto_merge true`, `auto_merge_min_confidence 0.6`.

## WebSocket `/ws` (server → client push)

Every message is `{"type": string, ...}`. On connect the server sends a `snapshot` first. Clients reconnect with backoff (1 s, 2 s, 5 s, max 10 s) and get a fresh snapshot. The server sends `{"type":"ping","t":<epoch>}` every 15 s. The client may send `{"type":"pong"}` (ignored) and the server tolerates any client text. Each client has a bounded send queue (500 messages). `levels`/`partial` are dropped first when a client is slow, and a client whose queue stays full for 10 s is disconnected.

| type | payload |
| --- | --- |
| `snapshot` | `recorder: Recorder, settings: Settings, segments: Segment[]` (newest first, at most 200), `meetings: Meeting[]` (those referenced by the segments), `speakers: Speaker[]`, `health: Health`, `jobs: Job[]` (active + last 20), `live: {segment_id: string \| null, lines: Line[]}` (at most the last 300 lines of the current segment), `activity: Activity[]` (last 100) |
| `recorder` | `recorder: Recorder` |
| `levels` | `levels: {mic?: {rms: number, peak: number}, system?: {rms: number, peak: number}}` (linear 0..1, same as helper events) |
| `partial` | `source, text, speaker: string \| null` (empty text clears) |
| `line` | `line: Line` |
| `segment` | `segment: Segment` (upsert) |
| `meeting` | `meeting: Meeting` (upsert) |
| `meeting_removed` | `id: string` |
| `segment_removed` | `id: string` (its meeting was deleted) |
| `speakers` | `speakers: Speaker[]` |
| `job` | `job: Job` (upsert) |
| `health` | `health: Health` (every 5 s) |
| `activity` | `activity: {ts: string, level: "info" \| "warning" \| "error", message: string}` |
| `settings` | `settings: Settings` |

## REST API (JSON; errors are `{"detail": "message"}` with 4xx/5xx)

| Method and path | Body / result |
| --- | --- |
| `GET /api/snapshot` | same as the WS snapshot |
| `POST /api/recording/start` | optional `{engine?, source?, mic?}` (persisted into settings; `mic` is a device name or UID, passed to the helper's `--mic` like the CLI) → `Recorder`. 409 if already recording |
| `POST /api/recording/stop` | → `Recorder` (closes current segment, enqueues jobs) |
| `POST /api/recording/split` | close the current segment now and start a new one without stopping the helper → `Recorder` |
| `GET /api/devices` | `{microphones: [{name, uid, default}], outputs: [...]}` |
| `GET/PUT /api/settings` | `Settings` |
| `GET /api/segments?before=<id>&limit=50` | `{segments: Segment[], meetings: Meeting[]}`: older pages (newest first) and the meetings they reference |
| `GET /api/segments/{id}` | `{segment, lines: Line[], transcript_md}` |
| `GET /api/meetings/{id}` | `{meeting, segments: Segment[], transcript_md, notes_md \| null}` |
| `PATCH /api/meetings/{id}` | `{title}` → `Meeting` |
| `POST /api/meetings/merge` | `{meeting_ids: string[]}` (2+). Result keeps the earliest meeting's id and title, `merged_by:"user"`, re-queues notes → `Meeting` |
| `POST /api/meetings/{id}/split` | split a merged meeting back into one meeting per segment; each gets notes re-queued unless its single-segment notes already exist → `Meeting[]` |
| `DELETE /api/meetings/{id}` | moves the meeting's segment folders (and `merged/<id>/`) to `trash/<time>_<id>/`, removes it from the index, broadcasts `segment_removed` + `meeting_removed` → `{deleted, trash}`. 409 while recording or while its notes are being generated |
| `POST /api/meetings/{id}/lines/delete` | `{lines: [{segment_id, source, id}]}` appends `{"type":"delete","source","id"}` events to each segment journal (append-only), re-renders `transcript.md`, sets `notes_stale` when notes exist → `{meeting, segments}`. 409 while a segment is recording |
| `GET /api/export/day/{YYYY-MM-DD}` | `text/markdown` attachment `voice-notes-<day>.md`: every meeting that started that local day, in order, with its notes (headings nested under the meeting title). 404 when the day has no meetings |
| `POST /api/meetings/{id}/refine` | re-queue notes → `Job` |
| `GET /api/speakers` / `POST /api/speakers/{id}/name {name}` / `POST /api/speakers/merge {source, target}` / `DELETE /api/speakers/{id}` | registry operations; naming/merging re-renders affected transcripts and broadcasts `speakers` |
| `GET /api/speakers/{id}/sample.wav` | `audio/wav` |
| `GET /api/health` | `Health` |
| `GET /api/jobs` | `Job[]` |

## LLM jobs (async, `jobs.py`)

- One asyncio queue, concurrency 1 (configurable), persisted to `jobs.json`. Retries: 3 attempts with backoff 30 s, 2 min, 10 min. Missing authentication is a permanent failure reported as `notes_status:"failed"` with the message. It is not retried in a tight loop.
- All Claude calls use the existing isolated worker process path (`refine.py`) so a hung SDK can be killed. Extend the worker request with an optional `system` prompt so the continuation check reuses it.
- **On segment close**: if the segment has speech and the previous segment (by start time) ended within 2 minutes of this one's start, has speech, and `settings.auto_merge` is on, then enqueue `continuation(segment)` first. Otherwise mark continuation `skipped` and enqueue `notes(meeting)`.
- **Continuation job**: send the last ~6000 chars of the previous meeting's transcript and the first ~6000 chars of this segment. System prompt asks for strict JSON `{"is_continuation": bool, "confidence": 0..1, "reason": "<one sentence>"}`. If the result is a continuation with confidence ≥ `auto_merge_min_confidence`, merge into the previous segment's meeting (`merged_by:"auto"`, `auto_merge_reason`) and enqueue `notes(merged meeting)`. Otherwise enqueue `notes(own meeting)`. A parse failure counts as "not a continuation" with `status:"failed"`.
- **Notes job** for a meeting: concatenate segment transcripts in order (each segment under a `## HH:MM–HH:MM` heading, timestamps shown as wall clock) and run `refine()`. Single-segment meetings write `notes.md` in the segment folder. Multi-segment meetings write to `merged/<id>/`. Coalesce: if a notes job for the same meeting is already queued, do not add another. If the meeting changed while running (merge/split), discard the result and let the newer job run.
- Empty segments (`status:"empty"`) get `notes_status:"skipped"` and never call the LLM.
- `--fake-llm`: continuation returns `is_continuation = true` iff both texts contain the same word that is 6+ letters long (deterministic test hook). Notes return `# Notes\n\n## Summary\n<first 200 chars>` after a 0.5 s sleep.

## Reliability

- **Supervisor** (`supervisor.py`): if the helper exits while recording is desired, log it, broadcast `recorder.state:"restarting"`, and restart with backoff 1, 2, 5, 10, 30, 60 s (reset after 10 min healthy). Keep the current segment open across helper restarts. After 10 consecutive failures within 15 minutes, set state `error` with the message, keep `recording_desired`, and retry every 5 minutes.
- **Startup deadline** as in the CLI (120 s, 600 s for fluid). A helper that does not become ready is killed and counted as a failure.
- **Stall detection**: while recording, if no `level` event has arrived for 60 s, treat the helper as hung: SIGTERM, then SIGKILL after 10 s, then restart.
- **Memory**: every 5 s sample server RSS and helper RSS (psutil). Warn above `server 1024 MB` / `helper 4096 MB` (configurable). If the helper stays above its limit for 5 minutes, restart it at the next quiet moment (no partial or line for 10 s) or at the next boundary, whichever comes first. If the server stays above 2048 MB for 5 minutes, log an error and exit with code 75 so launchd restarts it (state is persisted).
- **Bounded memory**: live line buffer 300, activity 100, id→segment map only for open segments, per-client queues bounded, segment list in snapshot capped at 200, never hold whole transcripts in memory except while rendering one.
- **Crash recovery on start**: segments left with status `recording`/`closing` are closed from their journals (truncated final line tolerated) and get jobs enqueued. `jobs.json` pending/running jobs are re-queued. If `state.json` says `recording_desired: true`, recording resumes automatically into a new segment and an `activity` warning says so.
- **Errors never kill the server**: every task is wrapped, exceptions are logged to `logs/server.log` and broadcast as `activity` errors, and counted in `errors_last_hour`.
- Signals: SIGTERM/SIGINT stop the helper gracefully (finalize, 20 s grace), close the open segment, persist state (keeping `recording_desired` as it was, so a launchd restart resumes), then exit.
- `service install` writes `~/Library/LaunchAgents/local.voice-notes.web.plist` (`RunAtLoad`, `KeepAlive`, `WorkingDirectory` = repo/cwd, `ProgramArguments` = the `uv run voice-notes serve` equivalent using the current venv's python `-m voice_notes.cli serve`, stdout/stderr to `~/Library/Logs/voice-notes/`), then `launchctl bootstrap gui/$UID`. `uninstall` does `bootout` and removes the plist. `status` prints `launchctl print` summary. Document that macOS microphone and screen-recording permission must be granted to the helper the first time.

## Frontend (`web/`, Vite + React + TypeScript)

- Single page, no router needed. Three areas:
  1. **Header / recorder bar**: Start / Stop / Split now buttons, state badge, engine + source + mic selectors (disabled while recording), current segment start and a countdown to `next_boundary_at`, mic and system level meters (smooth, dB scale -60..0), connection indicator (live / reconnecting).
  2. **Live panel**: partial text per source (greyed), finalized lines for the current segment with timestamp, speaker label and text. Auto-scroll unless the user scrolled up ("Jump to live" button).
  3. **Timeline / meetings list** grouped by day: each meeting is a card showing title (inline rename), time range, segment chips (with status, line count, continuation verdict + reason tooltip), notes status. Checkbox select + **Merge selected**, **Split** on merged meetings, **Regenerate notes**. Clicking a meeting opens a detail drawer with tabs Notes (rendered Markdown) / Transcript.
  4. **Side tabs**: **Speakers** (list, ▶ play sample via `<audio>`, name input, merge, forget with confirm dialog), **Health** (all `Health` fields, warnings highlighted, helper restarts, recent activity log, jobs list).
- State management: one reducer fed by WS messages, with upserts keyed by id. The UI must stay responsive all day: cap live lines at 300 and activity at 100, and throttle meter rendering with `requestAnimationFrame`.
- Plain CSS (CSS variables, light and dark via `prefers-color-scheme`). Dependencies: react, react-dom, react-markdown only (+ dev: vite, typescript, @vitejs/plugin-react, vitest, @testing-library/react, jsdom).
- `vite.config.ts`: `build.outDir = "../src/voice_notes/web/static"`, `emptyOutDir: true`; dev proxy for `/api` and `/ws` (ws: true) to `http://127.0.0.1:8765`.
- Add `data-testid` attributes for e2e tests: `start-btn`, `stop-btn`, `split-btn`, `recorder-state`, `level-mic`, `level-system`, `live-lines`, `live-line`, `partial-mic`, `partial-system`, `meeting-card`, `meeting-select`, `merge-btn`, `split-meeting-btn`, `refine-btn`, `notes-status`, `segment-chip`, `conn-status`, `health-panel`, `speaker-row`.
