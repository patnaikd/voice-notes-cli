// Mock Voice Notes backend for UI development. Node built-ins only.
//
//   node web/mock/server.mjs            # listens on 127.0.0.1:8799 (MOCK_PORT to change)
//   VOICE_NOTES_BACKEND=http://127.0.0.1:8799 npm --prefix web run dev
//
// Serves a snapshot, streams fake levels/partials/lines over /ws and answers
// the REST endpoints from docs/web-design.md with in-memory state.

import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:http";

const PORT = Number(process.env.MOCK_PORT ?? 8799);
const SEG_MIN = 30;

// ---------------------------------------------------------------- helpers

const pad = (n) => String(n).padStart(2, "0");
function iso(d) {
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`
  );
}
function folder(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}_${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
}
const hex = (n) => randomBytes(n).toString("hex").slice(0, n);
function boundaryFloor(d) {
  const b = new Date(d);
  b.setSeconds(0, 0);
  b.setMinutes(Math.floor(b.getMinutes() / SEG_MIN) * SEG_MIN);
  return b;
}
const addMin = (d, m) => new Date(d.getTime() + m * 60000);

// ---------------------------------------------------------------- state

const settings = {
  engine: "fluid",
  source: "both",
  mic: null,
  model: "claude-haiku-5-5",
  segment_minutes: SEG_MIN,
  auto_merge: true,
  auto_merge_min_confidence: 0.6,
};

const speakers = [
  { id: "spk-alice", name: "Alice Chen", display_name: "Alice Chen", sample_seconds: 8.4, created_at: iso(addMin(new Date(), -60 * 50)), has_sample: true },
  { id: "spk-bob", name: "Bob Ruiz", display_name: "Bob Ruiz", sample_seconds: 6.1, created_at: iso(addMin(new Date(), -60 * 30)), has_sample: true },
  { id: "spk-3f9a2c1d", name: null, display_name: "Unknown speaker 3f9a2c1d", sample_seconds: 4.2, created_at: iso(addMin(new Date(), -90)), has_sample: true },
  { id: "spk-77b0e4aa", name: null, display_name: "Unknown speaker 77b0e4aa", sample_seconds: 0, created_at: iso(addMin(new Date(), -20)), has_sample: false },
];

const clients = new Set(); // upgraded WebSocket sockets
const segments = new Map(); // id -> Segment
const meetings = new Map(); // id -> Meeting
const jobs = [];
const activity = [];
let liveLines = [];

function addSegment(start, end, over = {}) {
  const s = {
    id: folder(start),
    started_at: iso(start),
    ended_at: end ? iso(end) : null,
    status: end ? "closed" : "recording",
    line_count: end ? 20 + Math.floor(Math.random() * 120) : 0,
    meeting_id: "",
    engine: "fluid",
    continuation: { status: "skipped", is_continuation: null, confidence: null, reason: null, previous_segment_id: null },
    error: null,
    ...over,
  };
  segments.set(s.id, s);
  return s;
}
function addMeeting(title, segs, over = {}) {
  const m = {
    id: hex(12),
    title,
    segment_ids: segs.map((s) => s.id),
    started_at: segs[0].started_at,
    ended_at: segs[segs.length - 1].ended_at,
    notes_status: "done",
    notes_error: null,
    merged_by: null,
    auto_merge_reason: null,
    ...over,
  };
  for (const s of segs) s.meeting_id = m.id;
  meetings.set(m.id, m);
  return m;
}

const now = new Date();
const curStart = boundaryFloor(now);
// Yesterday
{
  const y = new Date(curStart);
  y.setDate(y.getDate() - 1);
  y.setHours(10, 0, 0, 0);
  const a = addSegment(y, addMin(y, 30));
  addMeeting("Design review: onboarding flow", [a]);
  const b = addSegment(addMin(y, 240), addMin(y, 270));
  const c = addSegment(addMin(y, 270), addMin(y, 300), {
    continuation: { status: "done", is_continuation: true, confidence: 0.91, reason: "Same participants continue discussing the Q3 hiring plan.", previous_segment_id: b.id },
  });
  addMeeting("Q3 hiring plan", [b, c], { merged_by: "auto", auto_merge_reason: "Same participants continue discussing the Q3 hiring plan." });
}
// Today, earlier
{
  const t = addMin(curStart, -180);
  const a = addSegment(t, addMin(t, 30));
  const b = addSegment(addMin(t, 30), addMin(t, 60), {
    continuation: { status: "done", is_continuation: true, confidence: 0.84, reason: "Continues the sprint planning discussion about the billing migration.", previous_segment_id: a.id },
  });
  addMeeting("Sprint planning", [a, b], { merged_by: "auto", auto_merge_reason: "Continues the sprint planning discussion about the billing migration." });
  const c = addSegment(addMin(t, 60), addMin(t, 90), {
    continuation: { status: "done", is_continuation: false, confidence: 0.88, reason: "Different topic: customer escalation call.", previous_segment_id: b.id },
  });
  addMeeting("Customer escalation – Acme", [c], { notes_status: "failed", notes_error: "Claude request timed out after 180 s (attempt 3 of 3)" });
  const d = addSegment(addMin(t, 90), addMin(t, 120), { status: "empty", line_count: 0 });
  addMeeting(`Meeting ${pad(addMin(t, 90).getHours())}:${pad(addMin(t, 90).getMinutes())}`, [d], { notes_status: "skipped" });
  const e = addSegment(addMin(t, 150), addMin(t, 180), {
    continuation: { status: "pending", is_continuation: null, confidence: null, reason: null, previous_segment_id: d.id },
  });
  addMeeting("1:1 with Bob", [e], { notes_status: "queued" });
}

const recorder = {
  state: "recording",
  segment_id: null,
  segment_started_at: null,
  next_boundary_at: null,
  engine: "fluid",
  source: "both",
  mic: null,
  helper_run: 2,
  helper_restarts: 1,
  message: null,
};

function openSegment(start) {
  const s = addSegment(start, null, { continuation: null });
  const m = addMeeting(`Meeting ${pad(start.getHours())}:${pad(start.getMinutes())}`, [s], { notes_status: "none", ended_at: null });
  recorder.segment_id = s.id;
  recorder.segment_started_at = s.started_at;
  recorder.next_boundary_at = iso(addMin(boundaryFloor(new Date()), SEG_MIN));
  liveLines = [];
  return [s, m];
}
openSegment(new Date(Math.max(curStart.getTime(), now.getTime() - 4 * 60000)));

const health = () => ({
  uptime_s: Math.floor(process.uptime()) + 6 * 3600,
  server_rss_mb: 180 + Math.random() * 20,
  helper_rss_mb: recorder.state === "idle" ? null : 1310 + Math.random() * 40,
  helper_pid: recorder.state === "idle" ? null : 48213,
  event_loop_lag_ms: Math.random() * 4,
  ws_clients: clients.size,
  jobs_pending: jobs.filter((j) => j.status === "queued").length,
  jobs_running: jobs.filter((j) => j.status === "running").length,
  errors_last_hour: 1,
  last_error: "Claude request timed out after 180 s (attempt 3 of 3)",
  disk_free_gb: 182.4,
  warnings: ["No system audio level for 120 s"],
});

function pushActivity(level, message) {
  const a = { ts: iso(new Date()), level, message };
  activity.push(a);
  if (activity.length > 100) activity.shift();
  broadcast({ type: "activity", activity: a });
}
pushActivity("info", "Server started (mock)");
pushActivity("warning", "Capture helper exited (code 1); restarting in 1 s");
pushActivity("info", "Helper run 2 ready (fluid)");
pushActivity("error", "Notes for “Customer escalation – Acme” failed: Claude request timed out");

function addJob(kind, target, status = "queued") {
  const j = { id: hex(10), kind, target, status, attempts: status === "queued" ? 0 : 1, error: null, created_at: iso(new Date()) };
  jobs.push(j);
  if (jobs.length > 40) jobs.shift();
  broadcast({ type: "job", job: j });
  return j;
}
for (const m of meetings.values()) if (m.notes_status === "done") jobs.push({ id: hex(10), kind: "notes", target: m.id, status: "done", attempts: 1, error: null, created_at: m.started_at });

function snapshot() {
  const segs = [...segments.values()].sort((a, b) => (a.started_at < b.started_at ? 1 : -1)).slice(0, 200);
  const mids = new Set(segs.map((s) => s.meeting_id));
  return {
    recorder,
    settings,
    segments: segs,
    meetings: [...meetings.values()].filter((m) => mids.has(m.id)),
    speakers,
    health: health(),
    jobs: jobs.slice(-30),
    live: { segment_id: recorder.segment_id, lines: liveLines.slice(-300) },
    activity: activity.slice(-100),
  };
}

// ---------------------------------------------------------------- websocket

function wsFrame(text) {
  const payload = Buffer.from(text);
  const len = payload.length;
  let header;
  if (len < 126) header = Buffer.from([0x81, len]);
  else if (len < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(len), 2);
  }
  return Buffer.concat([header, payload]);
}

function send(sock, msg) {
  if (!sock.destroyed) sock.write(wsFrame(JSON.stringify(msg)));
}
function broadcast(msg) {
  const frame = wsFrame(JSON.stringify(msg));
  for (const s of clients) if (!s.destroyed) s.write(frame);
}

// ---------------------------------------------------------------- fake stream

const SCRIPT = [
  ["Alice Chen", "mic", "Okay, let's pick up where we left off on the billing migration."],
  ["Bob Ruiz", "system", "Sure. The dry run finished last night with no reconciliation errors."],
  ["Alice Chen", "mic", "Great. What's left before we can switch the first cohort?"],
  ["Bob Ruiz", "system", "Mostly the webhook retries, and we still need sign-off from finance on the proration rules."],
  ["Unknown speaker 3f9a2c1d", "system", "I can take the proration review, I've got time Thursday afternoon."],
  ["Alice Chen", "mic", "Perfect. Let's also add a dashboard for failed charges so support can see them."],
  ["Bob Ruiz", "system", "Agreed. I'll file a ticket and link it in the channel."],
  ["Alice Chen", "mic", "And let's target next Tuesday for the first cohort, assuming the retries land."],
];
let scriptIdx = 0;
let lineSeq = 100;
let partial = null; // {source, speaker, words, shown}
let lvl = { mic: 0.05, system: 0.08 };

function recording() {
  return recorder.state === "recording";
}

setInterval(() => {
  if (!recording()) return;
  const talking = partial?.source;
  // Talking: roughly -24..-8 dB with syllable flutter; silence: room noise near -50 dB.
  const level = (on) => (on ? 0.06 + Math.random() * 0.3 : 0.002 + Math.random() * 0.004);
  lvl = { mic: lvl.mic * 0.4 + level(talking === "mic") * 0.6, system: lvl.system * 0.4 + level(talking === "system") * 0.6 };
  broadcast({
    type: "levels",
    levels: {
      mic: { rms: lvl.mic, peak: Math.min(1, lvl.mic * 2.2) },
      system: { rms: lvl.system, peak: Math.min(1, lvl.system * 2) },
    },
  });
}, 100);

setInterval(() => {
  if (!recording()) return;
  if (!partial) {
    const [speaker, source, text] = SCRIPT[scriptIdx % SCRIPT.length];
    scriptIdx++;
    partial = { speaker, source, words: text.split(" "), shown: 0 };
  }
  partial.shown += 1 + Math.floor(Math.random() * 2);
  if (partial.shown < partial.words.length) {
    broadcast({ type: "partial", source: partial.source, text: partial.words.slice(0, partial.shown).join(" "), speaker: null });
    return;
  }
  const seg = segments.get(recorder.segment_id);
  const t0 = (Date.now() - new Date(seg.started_at).getTime()) / 1000;
  const line = {
    segment_id: seg.id,
    id: `r${recorder.helper_run}-${lineSeq++}`,
    source: partial.source,
    start: Math.max(0, t0 - 4),
    end: t0,
    wall: iso(new Date(Date.now() - 4000)),
    text: partial.words.join(" "),
    speaker: partial.source === "mic" ? "Microphone" : "Remote audio",
    speaker_uuid: null,
  };
  const finalSpeaker = partial.speaker;
  broadcast({ type: "partial", source: partial.source, text: "", speaker: null });
  partial = null;
  liveLines.push(line);
  if (liveLines.length > 300) liveLines.shift();
  broadcast({ type: "line", line });
  // A speaker revision arrives a moment later with the same id.
  setTimeout(() => {
    const revised = { ...line, speaker: finalSpeaker, speaker_uuid: "x" };
    const i = liveLines.findIndex((l) => l.id === line.id);
    if (i >= 0) liveLines[i] = revised;
    broadcast({ type: "line", line: revised });
  }, 900);
  seg.line_count++;
  broadcast({ type: "segment", segment: seg });
}, 200);

setInterval(() => broadcast({ type: "health", health: health() }), 5000);
setInterval(() => clients.forEach((s) => send(s, { type: "ping", t: Date.now() / 1000 })), 15000);

// Finish queued notes jobs after a while.
setInterval(() => {
  const j = jobs.find((x) => x.status === "queued" || x.status === "running");
  if (!j) return;
  if (j.status === "queued") {
    j.status = "running";
    j.attempts++;
    const m = meetings.get(j.target);
    if (m && j.kind === "notes") {
      m.notes_status = "running";
      broadcast({ type: "meeting", meeting: m });
    }
  } else {
    j.status = "done";
    const m = meetings.get(j.target);
    if (m && j.kind === "notes") {
      m.notes_status = "done";
      broadcast({ type: "meeting", meeting: m });
      pushActivity("info", `Notes written for “${m.title}”`);
    }
  }
  broadcast({ type: "job", job: j });
}, 4000);

// ---------------------------------------------------------------- REST

function closeCurrent() {
  const s = segments.get(recorder.segment_id);
  if (!s) return;
  s.ended_at = iso(new Date());
  s.status = s.line_count > 0 ? "closed" : "empty";
  broadcast({ type: "segment", segment: s });
  const m = meetings.get(s.meeting_id);
  if (m) {
    m.ended_at = s.ended_at;
    m.notes_status = s.status === "empty" ? "skipped" : "queued";
    broadcast({ type: "meeting", meeting: m });
    if (s.status !== "empty") addJob("notes", m.id);
  }
}

function meetingDetail(m) {
  const segs = m.segment_ids.map((id) => segments.get(id)).filter(Boolean);
  const transcript = segs
    .map((s) => {
      const st = new Date(s.started_at);
      const en = s.ended_at ? new Date(s.ended_at) : new Date();
      return `## ${pad(st.getHours())}:${pad(st.getMinutes())}–${pad(en.getHours())}:${pad(en.getMinutes())}\n\n` +
        SCRIPT.map(([sp, , t], i) => `**${pad(st.getHours())}:${pad(st.getMinutes() + i)}** ${sp}: ${t}`).join("\n\n");
    })
    .join("\n\n");
  const notes = m.notes_status === "done"
    ? `# ${m.title}\n\n## Summary\nThe team reviewed progress on the billing migration and agreed on a target date for the first cohort.\n\n## Decisions\n- Target **next Tuesday** for cohort 1, assuming webhook retries land.\n- Add a failed-charges dashboard for support.\n\n## Action items\n- [ ] Bob: file the dashboard ticket\n- [ ] Unknown speaker 3f9a2c1d: review proration rules (Thursday)\n- [ ] Alice: confirm finance sign-off\n\n## Open questions\n- Do we need a rollback plan per cohort?`
    : null;
  return { meeting: m, segments: segs, transcript_md: transcript, notes_md: notes };
}

function wav() {
  const rate = 16000, secs = 1.2, n = Math.floor(rate * secs);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write("WAVE", 8); buf.write("fmt ", 12);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write("data", 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 220 * i) / rate) * 6000 * Math.min(1, (n - i) / 2000)), 44 + i * 2);
  return buf;
}

async function body(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const text = Buffer.concat(chunks).toString();
  return text ? JSON.parse(text) : {};
}

function json(res, status, data) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}
const err = (res, status, detail) => json(res, status, { detail });

const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;
  const m = req.method;
  let match;
  try {
    if (m === "GET" && p === "/api/snapshot") return json(res, 200, snapshot());
    if (m === "GET" && p === "/api/health") return json(res, 200, health());
    if (m === "GET" && p === "/api/jobs") return json(res, 200, jobs);
    if (m === "GET" && p === "/api/devices")
      return json(res, 200, {
        microphones: [
          { name: "MacBook Pro Microphone", uid: "BuiltInMicrophoneDevice", default: true },
          { name: "HyperX SoloCast 2", uid: "AppleUSBAudioEngine:HyperX", default: false },
        ],
        outputs: [{ name: "MacBook Pro Speakers", uid: "BuiltInSpeakerDevice", default: true }],
      });
    if (p === "/api/settings") {
      if (m === "PUT") {
        if (recorder.state !== "idle") return err(res, 409, "Settings cannot change while recording");
        Object.assign(settings, await body(req));
        broadcast({ type: "settings", settings });
      }
      return json(res, 200, settings);
    }
    if (m === "POST" && p === "/api/recording/start") {
      if (recorder.state !== "idle") return err(res, 409, "Already recording");
      const b = await body(req);
      Object.assign(settings, Object.fromEntries(Object.entries(b).filter(([, v]) => v !== undefined)));
      recorder.state = "starting";
      recorder.engine = settings.engine;
      recorder.source = settings.source;
      recorder.mic = settings.mic;
      broadcast({ type: "recorder", recorder });
      setTimeout(() => {
        const [s, mt] = openSegment(new Date());
        recorder.state = "recording";
        recorder.helper_run++;
        broadcast({ type: "segment", segment: s });
        broadcast({ type: "meeting", meeting: mt });
        broadcast({ type: "recorder", recorder });
        pushActivity("info", `Recording started (${settings.engine})`);
      }, 1500);
      return json(res, 200, recorder);
    }
    if (m === "POST" && p === "/api/recording/stop") {
      if (recorder.state === "idle") return err(res, 409, "Not recording");
      recorder.state = "stopping";
      broadcast({ type: "recorder", recorder });
      setTimeout(() => {
        closeCurrent();
        Object.assign(recorder, { state: "idle", segment_id: null, segment_started_at: null, next_boundary_at: null });
        partial = null;
        broadcast({ type: "partial", source: "mic", text: "", speaker: null });
        broadcast({ type: "partial", source: "system", text: "", speaker: null });
        broadcast({ type: "recorder", recorder });
        pushActivity("info", "Recording stopped");
      }, 1000);
      return json(res, 200, recorder);
    }
    if (m === "POST" && p === "/api/recording/split") {
      if (recorder.state !== "recording") return err(res, 409, "Not recording");
      closeCurrent();
      const [s, mt] = openSegment(new Date());
      broadcast({ type: "segment", segment: s });
      broadcast({ type: "meeting", meeting: mt });
      broadcast({ type: "recorder", recorder });
      pushActivity("info", "Segment split by user");
      return json(res, 200, recorder);
    }
    if (m === "GET" && p === "/api/segments") {
      const before = url.searchParams.get("before");
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const all = [...segments.values()].sort((a, b) => (a.id < b.id ? 1 : -1));
      // Synthesize one older day the first time it is asked for.
      if (!globalThis.__olderMade) {
        globalThis.__olderMade = true;
        const d = new Date(curStart);
        d.setDate(d.getDate() - 3);
        d.setHours(9, 0, 0, 0);
        const a = addSegment(d, addMin(d, 30));
        addMeeting("Quarterly planning kickoff", [a]);
        const b = addSegment(addMin(d, 120), addMin(d, 150));
        addMeeting("Vendor call – Northwind", [b]);
        return json(res, 200, [b, a]);
      }
      // After that page, report the end of history (only the synthesized day is older than the snapshot).
      return json(res, 200, all.filter((s) => !before || s.id < before).slice(0, limit).filter((s) => s.started_at < iso(addMin(curStart, -60 * 24 * 3))));
    }
    if (m === "GET" && (match = p.match(/^\/api\/segments\/([^/]+)$/))) {
      const s = segments.get(decodeURIComponent(match[1]));
      if (!s) return err(res, 404, "Segment not found");
      return json(res, 200, { segment: s, lines: [], transcript_md: "" });
    }
    if (m === "POST" && p === "/api/meetings/merge") {
      const { meeting_ids } = await body(req);
      if (!Array.isArray(meeting_ids) || meeting_ids.length < 2) return err(res, 400, "Select at least two meetings");
      const ms = meeting_ids.map((id) => meetings.get(id));
      if (ms.some((x) => !x)) return err(res, 404, "Meeting not found");
      ms.sort((a, b) => (a.started_at < b.started_at ? -1 : 1));
      const keep = ms[0];
      const segIds = ms.flatMap((x) => x.segment_ids).sort();
      keep.segment_ids = segIds;
      keep.ended_at = ms.map((x) => x.ended_at).sort().at(-1) ?? null;
      keep.merged_by = "user";
      keep.notes_status = "queued";
      for (const id of segIds) {
        const s = segments.get(id);
        s.meeting_id = keep.id;
        broadcast({ type: "segment", segment: s });
      }
      for (const x of ms.slice(1)) {
        meetings.delete(x.id);
        broadcast({ type: "meeting_removed", id: x.id });
      }
      broadcast({ type: "meeting", meeting: keep });
      addJob("notes", keep.id);
      pushActivity("info", `Merged ${ms.length} meetings into “${keep.title}”`);
      return json(res, 200, keep);
    }
    if ((match = p.match(/^\/api\/meetings\/([^/]+)(\/split|\/refine)?$/))) {
      const id = decodeURIComponent(match[1]);
      const mt = meetings.get(id);
      if (!mt) return err(res, 404, "Meeting not found");
      if (m === "GET" && !match[2]) return json(res, 200, meetingDetail(mt));
      if (m === "PATCH" && !match[2]) {
        const { title } = await body(req);
        if (!title || !title.trim()) return err(res, 422, "Title must not be empty");
        mt.title = title.trim();
        broadcast({ type: "meeting", meeting: mt });
        return json(res, 200, mt);
      }
      if (m === "POST" && match[2] === "/refine") {
        mt.notes_status = "queued";
        broadcast({ type: "meeting", meeting: mt });
        return json(res, 200, addJob("notes", mt.id));
      }
      if (m === "POST" && match[2] === "/split") {
        if (mt.segment_ids.length < 2) return err(res, 409, "Meeting has a single segment");
        const out = [];
        const [first, ...rest] = mt.segment_ids.map((sid) => segments.get(sid));
        mt.segment_ids = [first.id];
        mt.ended_at = first.ended_at;
        mt.merged_by = null;
        mt.auto_merge_reason = null;
        out.push(mt);
        broadcast({ type: "meeting", meeting: mt });
        for (const s of rest) {
          const nm = addMeeting(`Meeting ${s.id.slice(11, 13)}:${s.id.slice(14, 16)}`, [s], { notes_status: "queued" });
          broadcast({ type: "segment", segment: s });
          broadcast({ type: "meeting", meeting: nm });
          addJob("notes", nm.id);
          out.push(nm);
        }
        return json(res, 200, out);
      }
    }
    if (m === "GET" && p === "/api/speakers") return json(res, 200, speakers);
    if (m === "POST" && p === "/api/speakers/merge") {
      const { source, target } = await body(req);
      const i = speakers.findIndex((s) => s.id === source);
      if (i < 0 || !speakers.some((s) => s.id === target)) return err(res, 404, "Speaker not found");
      speakers.splice(i, 1);
      broadcast({ type: "speakers", speakers });
      return json(res, 200, { ok: true });
    }
    if ((match = p.match(/^\/api\/speakers\/([^/]+)(\/name|\/sample\.wav)?$/))) {
      const id = decodeURIComponent(match[1]);
      const sp = speakers.find((s) => s.id === id);
      if (!sp) return err(res, 404, "Speaker not found");
      if (m === "GET" && match[2] === "/sample.wav") {
        res.writeHead(200, { "Content-Type": "audio/wav" });
        return res.end(wav());
      }
      if (m === "POST" && match[2] === "/name") {
        const { name } = await body(req);
        if (!name || !name.trim()) return err(res, 422, "Name must not be empty");
        sp.name = name.trim();
        sp.display_name = sp.name;
        broadcast({ type: "speakers", speakers });
        return json(res, 200, sp);
      }
      if (m === "DELETE" && !match[2]) {
        speakers.splice(speakers.indexOf(sp), 1);
        broadcast({ type: "speakers", speakers });
        return json(res, 200, { ok: true });
      }
    }
    return err(res, 404, `No route for ${m} ${p}`);
  } catch (e) {
    return err(res, 500, String(e));
  }
});

server.on("upgrade", (req, sock) => {
  if (new URL(req.url, "http://x").pathname !== "/ws") return sock.destroy();
  const key = req.headers["sec-websocket-key"];
  const accept = createHash("sha1").update(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
  sock.write(
    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" + `Sec-WebSocket-Accept: ${accept}\r\n\r\n`,
  );
  sock.setNoDelay(true);
  clients.add(sock);
  send(sock, { type: "snapshot", ...snapshot() });
  sock.on("data", (buf) => {
    // Only the opcode matters here: 0x8 = close. Client text (pongs) is ignored.
    if ((buf[0] & 0x0f) === 0x8) sock.end();
  });
  const drop = () => clients.delete(sock);
  sock.on("close", drop);
  sock.on("error", drop);
});

server.listen(PORT, "127.0.0.1", () => console.log(`mock backend on http://127.0.0.1:${PORT}`));
