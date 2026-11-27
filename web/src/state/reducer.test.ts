import { describe, expect, it } from "vitest";
import { groupMeetings, orphanSegments } from "../lib/grouping";
import { line, meeting, seg, snapshot } from "../test/fixtures";
import { ACTIVITY_CAP, initialState, LIVE_LINES_CAP, reducer, type AppState } from "./reducer";

function withSnapshot(over = {}): AppState {
  return reducer(initialState, { type: "snapshot", ...snapshot(over) });
}

describe("reducer", () => {
  it("loads a snapshot and keys segments and meetings by id", () => {
    const s = withSnapshot({
      segments: [seg("s1"), seg("s2", { meeting_id: "m2" })],
      meetings: [meeting("m1", { segment_ids: ["s1"] }), meeting("m2", { segment_ids: ["s2"] })],
      live: { segment_id: "s2", lines: [line("a")] },
    });
    expect(s.hasSnapshot).toBe(true);
    expect(Object.keys(s.segments)).toEqual(["s1", "s2"]);
    expect(s.meetings.m2.segment_ids).toEqual(["s2"]);
    expect(s.live.lines).toHaveLength(1);
    expect(s.recorder?.state).toBe("idle");
  });

  it("resets previous state on a fresh snapshot (reconnect)", () => {
    let s = withSnapshot({ segments: [seg("old")], meetings: [meeting("mold")] });
    s = reducer(s, { type: "partial", source: "mic", text: "hello", speaker: null });
    s = reducer(s, { type: "snapshot", ...snapshot({ segments: [seg("new")] }) });
    expect(Object.keys(s.segments)).toEqual(["new"]);
    expect(s.meetings).toEqual({});
    expect(s.partials.mic).toBeNull();
  });

  it("caps a snapshot's live lines and activity", () => {
    const lines = Array.from({ length: 400 }, (_, i) => line(`l${i}`));
    const activity = Array.from({ length: 150 }, (_, i) => ({ ts: `t${i}`, level: "info" as const, message: `m${i}` }));
    const s = withSnapshot({ live: { segment_id: "s1", lines }, activity });
    expect(s.live.lines).toHaveLength(LIVE_LINES_CAP);
    expect(s.live.lines[0].id).toBe("l100");
    expect(s.activity).toHaveLength(ACTIVITY_CAP);
    expect(s.activity[ACTIVITY_CAP - 1].message).toBe("m149");
  });

  it("upserts segments, meetings and jobs by id", () => {
    let s = withSnapshot({ segments: [seg("s1")], meetings: [meeting("m1")] });
    s = reducer(s, { type: "segment", segment: seg("s1", { line_count: 42 }) });
    s = reducer(s, { type: "segment", segment: seg("s2") });
    s = reducer(s, { type: "meeting", meeting: meeting("m1", { title: "Renamed" }) });
    const job = { id: "j1", kind: "notes" as const, target: "m1", status: "queued" as const, attempts: 0, error: null, created_at: "x" };
    s = reducer(s, { type: "job", job });
    s = reducer(s, { type: "job", job: { ...job, status: "done" } });
    expect(s.segments.s1.line_count).toBe(42);
    expect(Object.keys(s.segments)).toHaveLength(2);
    expect(s.meetings.m1.title).toBe("Renamed");
    expect(s.jobs).toHaveLength(1);
    expect(s.jobs[0].status).toBe("done");
  });

  it("replaces a line with the same id in place", () => {
    let s = withSnapshot();
    s = reducer(s, { type: "line", line: line("a") });
    s = reducer(s, { type: "line", line: line("b") });
    s = reducer(s, { type: "line", line: line("a", { speaker: "Alice", text: "revised" }) });
    expect(s.live.lines.map((l) => l.id)).toEqual(["a", "b"]);
    expect(s.live.lines[0].speaker).toBe("Alice");
    expect(s.live.lines[0].text).toBe("revised");
  });

  it("caps live lines at 300 and activity at 100", () => {
    let s = withSnapshot();
    for (let i = 0; i < 350; i++) s = reducer(s, { type: "line", line: line(`l${i}`) });
    for (let i = 0; i < 130; i++) s = reducer(s, { type: "activity", activity: { ts: `${i}`, level: "info", message: `m${i}` } });
    expect(s.live.lines).toHaveLength(300);
    expect(s.live.lines[0].id).toBe("l50");
    expect(s.live.lines[299].id).toBe("l349");
    expect(s.activity).toHaveLength(100);
    expect(s.activity[0].message).toBe("m30");
  });

  it("removes a meeting on meeting_removed", () => {
    let s = withSnapshot({ meetings: [meeting("m1"), meeting("m2")] });
    s = reducer(s, { type: "meeting_removed", id: "m2" });
    expect(Object.keys(s.meetings)).toEqual(["m1"]);
    expect(reducer(s, { type: "meeting_removed", id: "missing" })).toBe(s);
  });

  it("removes a segment on segment_removed", () => {
    let s = withSnapshot({ segments: [seg("s1"), seg("s2")] });
    s = reducer(s, { type: "segment_removed", id: "s1" });
    expect(Object.keys(s.segments)).toEqual(["s2"]);
    expect(reducer(s, { type: "segment_removed", id: "missing" })).toBe(s);
  });

  it("tracks partials and clears them on a finalized line", () => {
    let s = withSnapshot();
    s = reducer(s, { type: "partial", source: "system", text: "hel", speaker: null });
    expect(s.partials.system?.text).toBe("hel");
    const same = reducer(s, { type: "partial", source: "system", text: "hel", speaker: null });
    expect(same).toBe(s);
    s = reducer(s, { type: "line", line: line("x", { source: "system" }) });
    expect(s.partials.system).toBeNull();
    s = reducer(s, { type: "partial", source: "mic", text: "yo", speaker: null });
    s = reducer(s, { type: "partial", source: "mic", text: "", speaker: null });
    expect(s.partials.mic).toBeNull();
  });

  it("ignores levels and pings without changing state", () => {
    const s = withSnapshot();
    expect(reducer(s, { type: "levels", levels: { mic: { rms: 0.1, peak: 0.2 } } })).toBe(s);
    expect(reducer(s, { type: "ping", t: 1 })).toBe(s);
  });

  it("merges older pages of segments and replaces meetings after split", () => {
    let s = withSnapshot({ segments: [seg("s3")], meetings: [meeting("m1", { segment_ids: ["s1", "s2"] })] });
    s = reducer(s, { type: "segments_loaded", segments: [seg("s1"), seg("s2")], exhausted: true });
    expect(Object.keys(s.segments).sort()).toEqual(["s1", "s2", "s3"]);
    expect(s.olderExhausted).toBe(true);
    s = reducer(s, {
      type: "meetings_replaced",
      removed: ["m1"],
      meetings: [meeting("m1", { segment_ids: ["s1"] }), meeting("m9", { segment_ids: ["s2"] })],
    });
    expect(s.meetings.m1.segment_ids).toEqual(["s1"]);
    expect(s.meetings.m9.segment_ids).toEqual(["s2"]);
  });

  it("caps toasts", () => {
    let s = initialState;
    for (let i = 0; i < 9; i++) s = reducer(s, { type: "toast", level: "error", message: `e${i}` });
    expect(s.toasts).toHaveLength(5);
    s = reducer(s, { type: "toast_dismiss", id: s.toasts[0].id });
    expect(s.toasts).toHaveLength(4);
  });
});

describe("groupMeetings", () => {
  it("groups by day newest first and only adds placeholders for uncovered segments", () => {
    const meetings = {
      a: meeting("a", { started_at: "2026-11-25T09:00:00-08:00", segment_ids: ["s1", "s2"] }),
      b: meeting("b", { started_at: "2026-11-24T09:00:00-08:00", segment_ids: ["s3"] }),
    };
    const segments = {
      s1: seg("s1", { meeting_id: "a" }),
      s2: seg("s2", { meeting_id: "gone" }), // stale meeting_id, but covered by "a"
      s4: seg("s4", { meeting_id: "unknown", started_at: "2026-11-25T11:00:00-08:00" }),
    };
    const groups = groupMeetings(meetings, orphanSegments(meetings, segments));
    expect(groups).toHaveLength(2);
    expect(groups[0].meetings.map((m) => m.id)).toEqual(["unknown", "a"]);
    expect(groups[1].meetings.map((m) => m.id)).toEqual(["b"]);
  });

  it("orders by instant, not by text, when UTC offsets differ", () => {
    // The DST fall-back hour: 01:15-08:00 (09:15Z) is newer than 01:30-07:00 (08:30Z), though it sorts lower as text.
    const meetings = {
      pdt: meeting("pdt", { started_at: "2026-11-01T01:30:00-07:00", segment_ids: [] }),
      pst: meeting("pst", { started_at: "2026-11-01T01:15:00-08:00", segment_ids: [] }),
    };
    const groups = groupMeetings(meetings, []);
    expect(groups[0].meetings.map((m) => m.id)).toEqual(["pst", "pdt"]);
  });
});
