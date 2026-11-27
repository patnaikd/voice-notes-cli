"""Web server: segment math, routing, supervisor recovery, jobs, REST and WebSocket."""
import asyncio
import json
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voice_notes.web import app as web_app
from voice_notes.web.hub import Client, Hub
from voice_notes.web.jobs import fake_verdict, parse_verdict
from voice_notes.web.recorder import next_boundary
from voice_notes.web.store import Store

FAKE = Path(__file__).with_name("fake_helper.py")
HELPER = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))}"


def wait_for(predicate, timeout=10.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {message}")


def make_config(tmp_path, **overrides):
    values = dict(data_dir=tmp_path / "meetings", speakers_dir=tmp_path / "speakers", helper_command=HELPER,
                  fake_llm=True, segment_seconds=3600, grace_seconds=0.3, stop_grace=5, kill_timeout=1,
                  health_interval=0.5, job_backoff=(0.2, 0.2, 0.2))
    values.update(overrides)
    return web_app.Config(**values)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    clients = []

    def start(**overrides):
        server = web_app.Server(make_config(tmp_path, **overrides))
        client = TestClient(web_app.create_app(server))
        client.__enter__()
        clients.append(client)
        return server, client

    yield start
    for client in clients:
        client.__exit__(None, None, None)


def write_segment(root: Path, name: str, lines: list[str], *, start=None, kind=None, truncated=False):
    directory = root / name
    directory.mkdir(parents=True)
    started = start or datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").astimezone().isoformat()
    info = {"title": "Meeting", "started_at": started, "engine": "fluid"}
    if kind:
        info.update(kind=kind, segment_start=started, segment_end=None)
    (directory / "meeting.json").write_text(json.dumps(info))
    events = [json.dumps(dict(type="segment", source="mic", id=f"r1-u{i}", start=float(i), end=i + 0.5, text=text))
              for i, text in enumerate(lines)]
    if truncated:
        events.append('{"type": "segment", "source": "mic", "id": "r1-u99", "sta')
    (directory / "events.jsonl").write_text("\n".join(events) + ("\n" if events and not truncated else ""))
    return directory


# Segment boundaries


@pytest.fixture
def los_angeles(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def local(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def wall(epoch):
    return time.strftime("%H:%M:%S %Z", time.localtime(epoch))


def test_boundaries_are_multiples_since_local_midnight(los_angeles):
    assert wall(next_boundary(local("2026-11-25 14:07:12"), 1800)) == "14:30:00 PST"
    assert wall(next_boundary(local("2026-11-25 14:30:00"), 1800)) == "15:00:00 PST"
    assert wall(next_boundary(local("2026-11-25 14:00:05"), 20)) == "14:00:20 PST"
    # A 7-minute grid does not divide a day; the last segment ends at midnight.
    assert time.strftime("%d %H:%M", time.localtime(next_boundary(local("2026-11-25 23:58:00"), 420))) == "26 00:00"


def test_boundaries_across_dst_changes(los_angeles):
    # Spring forward: 02:00 PST jumps to 03:00 PDT.
    assert wall(next_boundary(local("2026-03-08 01:50:00"), 1800)) == "03:00:00 PDT"
    assert wall(next_boundary(local("2026-03-08 03:10:00"), 1800)) == "03:30:00 PDT"
    # Fall back: 02:00 PDT becomes 01:00 PST; boundaries keep advancing in real time.
    first = next_boundary(local("2026-11-01 00:50:00"), 1800)
    second = next_boundary(first, 1800)
    third = next_boundary(second, 1800)
    assert [wall(t) for t in (first, second, third)] == ["01:00:00 PDT", "01:30:00 PDT", "01:00:00 PST"]
    assert second - first == 1800 and third - second == 1800


# Routing


def test_late_finals_route_to_previous_segment_and_ids_are_rewritten(tmp_path):
    async def scenario():
        server = web_app.Server(make_config(tmp_path, grace_seconds=60))
        recorder, store = server.recorder, server.store
        t0 = time.time() - 100
        recorder._open_segment(t0)
        first = recorder.current
        recorder._roll(t0 + 50)
        second = recorder.current
        line = dict(type="segment", source="mic", end=0, text="x")
        recorder._route({**line, "id": "u1", "start": 45.0, "end": 49.0, "text": "Late final."}, 1, t0)
        recorder._route({**line, "id": "u2", "start": 55.0, "end": 56.0, "text": "New segment."}, 1, t0)
        # Speaker revision of u1 stays in the first segment even though its time moved.
        recorder._route({**line, "id": "u1", "start": 51.0, "end": 52.0, "text": "Late final, revised."}, 1, t0)
        # After a helper restart the ids restart too, but the run prefix keeps them unique.
        recorder._route({**line, "id": "u1", "start": 2.0, "end": 3.0, "text": "Second run."}, 2, t0 + 60)
        assert first.line_ids == {"r1-u1"} and second.line_ids == {"r1-u2", "r2-u1"}
        lines = {l["id"]: l for l in store.segment_lines(second.record)}
        assert lines["r1-u2"]["start"] == pytest.approx(5.0)
        assert lines["r2-u1"]["start"] == pytest.approx(12.0)
        assert [l["text"] for l in store.segment_lines(first.record)] == ["Late final, revised."]
        assert [l["id"] for l in recorder.live] == ["r1-u2", "r2-u1"]
        await recorder._close(first, first.end)
        assert first.record["status"] == "closed" and first.record["line_count"] == 1
        assert not any(value is first for value in recorder.ids.values())
        assert "Late final, revised." in (store.segment_dir(first.id) / "transcript.md").read_text()
        await recorder._close(second, time.time())
        await server.jobs.close()
    asyncio.run(scenario())


def test_revision_for_a_closed_segment_is_not_duplicated(tmp_path):
    async def scenario():
        server = web_app.Server(make_config(tmp_path, grace_seconds=60))
        recorder, store = server.recorder, server.store
        t0 = time.time() - 100
        recorder._open_segment(t0)
        first = recorder.current
        line = dict(type="segment", source="system", text="x")
        recorder._route({**line, "id": "u1", "start": 40.0, "end": 45.0, "text": "Before the boundary."}, 1, t0)
        recorder._roll(t0 + 50)
        second = recorder.current
        await recorder._close(first, first.end)
        # The helper revises u1's speaker after its segment closed (e.g. right after a restart).
        recorder._route({**line, "id": "u1", "start": 55.0, "end": 56.0, "text": "Before the boundary.",
                         "speaker_uuid": "3f2a91c4-0000-4000-8000-000000000001"}, 1, t0)
        assert second.line_ids == set() and second.record["line_count"] == 0
        assert list(recorder.live) == [] and store.segment_lines(second.record) == []
        assert [l["text"] for l in store.segment_lines(first.record)] == ["Before the boundary."]
        await recorder._close(second, time.time())
        await server.jobs.close()
    asyncio.run(scenario())


# End-to-end with the fake helper


def test_record_roll_auto_merge_and_stop(serve):
    server, client = serve(segment_seconds=2, grace_seconds=0.4)
    assert client.post("/api/recording/start", json={"engine": "apple"}).json()["state"] in ("starting", "recording")
    assert client.post("/api/recording/start").status_code == 409
    wait_for(lambda: server.recorder.state == "recording", message="recording")
    closed = lambda: [s for s in server.store.segments.values() if s["status"] == "closed"]
    wait_for(lambda: len(closed()) >= 2, timeout=15, message="two closed segments")
    wait_for(lambda: any(m["merged_by"] == "auto" for m in server.store.meetings.values()), timeout=10,
             message="auto merge")
    recorder = client.post("/api/recording/stop").json()
    assert recorder["state"] == "idle" and recorder["segment_id"] is None
    assert not any(s["status"] in ("recording", "closing") for s in server.store.segments.values())
    merged = next(m for m in server.store.meetings.values() if m["merged_by"] == "auto")
    assert merged["auto_merge_reason"] and len(merged["segment_ids"]) >= 2
    segment = server.store.segments[merged["segment_ids"][1]]
    assert segment["continuation"]["is_continuation"] is True
    wait_for(lambda: server.store.meetings[merged["id"]]["notes_status"] == "done", timeout=15, message="notes")
    notes = server.store.root / "merged" / merged["id"] / "notes.md"
    assert notes.read_text().startswith("# Notes")
    meta = json.loads((server.store.segment_dir(segment["id"]) / "meeting.json").read_text())
    assert meta["kind"] == "segment" and meta["segment_end"]
    assert not server.store.recording_desired
    detail = client.get(f"/api/meetings/{merged['id']}").json()
    assert detail["notes_md"].startswith("# Notes") and "## " in detail["transcript_md"]


def test_split_now_and_segment_detail(serve):
    server, client = serve()
    client.post("/api/recording/start")
    wait_for(lambda: server.recorder.current and server.recorder.current.line_ids, message="first line")
    first = server.recorder.current.id
    after = client.post("/api/recording/split").json()
    assert after["segment_id"] != first
    wait_for(lambda: server.store.segments[first]["status"] == "closed", message="split segment closed")
    detail = client.get(f"/api/segments/{first}").json()
    assert detail["lines"] and detail["lines"][0]["id"].startswith("r1-") and "## Transcript" in detail["transcript_md"]
    client.post("/api/recording/stop")
    assert client.post("/api/recording/split").status_code == 409


def test_helper_crash_restarts_and_keeps_segment_open(serve, tmp_path):
    marker = tmp_path / "crashed"
    server, client = serve(helper_command=f"{HELPER} --crash-once {shlex.quote(str(marker))}")
    client.post("/api/recording/start")
    segment = wait_for(lambda: server.recorder.current and server.recorder.current.id)
    wait_for(lambda: server.recorder.supervisor.restarts >= 1, message="restart")
    wait_for(lambda: server.recorder.state == "recording" and server.recorder.supervisor.run == 2, message="rerun")
    wait_for(lambda: any(i.startswith("r2-") for i in server.recorder.current.line_ids), message="run-2 lines")
    assert server.recorder.current.id == segment
    assert server.store.segments[segment]["status"] == "recording"
    assert any("restarting" in a["message"] for a in server.activity_log)
    client.post("/api/recording/stop")


def test_stalled_helper_is_killed_and_restarted(serve):
    server, client = serve(helper_command=f"{HELPER} --hang-after 0.3 --ignore-term", stall_seconds=0.8)
    client.post("/api/recording/start")
    wait_for(lambda: server.recorder.supervisor.restarts >= 1, timeout=10, message="stall restart")
    assert any("hung" in a["message"] for a in server.activity_log)
    client.post("/api/recording/stop")
    assert server.recorder.supervisor.pid is None


def test_crash_recovery_on_startup(serve, tmp_path):
    root = tmp_path / "meetings"
    name = "2026-11-25_09-00-00"
    write_segment(root, name, ["We agreed on the budget."], kind="segment", truncated=True)
    store = Store(root)
    segment = {"id": name, "started_at": datetime(2026, 11, 25, 9).astimezone().isoformat(), "ended_at": None,
               "status": "recording", "line_count": 0, "meeting_id": "", "engine": "fluid", "continuation": None,
               "error": None}
    store.segments[name] = segment
    store.new_meeting(segment)
    write_segment(root, "2026-11-24_10-00-00", ["Old CLI meeting."])
    old = {"id": "2026-11-24_10-00-00", "started_at": "2026-11-24T10:00:00+00:00", "ended_at": None,
           "status": "closed", "line_count": 1, "meeting_id": "", "engine": "apple", "continuation": None,
           "error": None}
    store.segments[old["id"]] = old
    old_meeting = store.new_meeting(old)
    store.save_index()
    store.jobs = [{"id": "abc", "kind": "notes", "target": old_meeting["id"], "status": "running", "attempts": 1,
                   "error": None, "created_at": "2026-11-24T10:00:00+00:00", "not_before": 0}]
    store.save_jobs()
    store.recording_desired = True
    store.save_state()
    server, client = serve()
    recovered = server.store.segments[name]
    assert recovered["status"] == "closed" and recovered["line_count"] == 1 and recovered["ended_at"]
    wait_for(lambda: server.store.meetings[recovered["meeting_id"]]["notes_status"] == "done", message="notes")
    wait_for(lambda: next(j for j in server.store.jobs if j["id"] == "abc")["status"] == "done", message="requeued")
    wait_for(lambda: server.recorder.state == "recording", message="resumed recording")
    assert any("resumed" in a["message"] and a["level"] == "warning" for a in server.activity_log)
    assert any("Recovered segment" in a["message"] for a in server.activity_log)


def test_legacy_import_merge_split_rename_and_discard(serve, tmp_path):
    root = tmp_path / "meetings"
    write_segment(root, "2026-11-24_10-00-00", ["Planning the roadmap."])
    write_segment(root, "2026-11-24_11-00-00", ["Reviewing the roadmap."])
    write_segment(root, "2026-11-24_12-00-00", [])
    (root / "2026-11-24_10-00-00" / "notes.md").write_text("# Old notes\n")
    server, client = serve()
    meetings = {m["segment_ids"][0]: m for m in client.get("/api/snapshot").json()["meetings"]}
    a, b, empty = (meetings[k] for k in ("2026-11-24_10-00-00", "2026-11-24_11-00-00", "2026-11-24_12-00-00"))
    assert a["notes_status"] == "done" and b["notes_status"] == "none" and empty["notes_status"] == "skipped"
    assert client.post("/api/meetings/merge", json={"meeting_ids": [a["id"]]}).status_code == 400
    assert client.post("/api/meetings/nope/refine").status_code == 404
    # Notes for B start running; merging B into A must discard that result.
    job = client.post(f"/api/meetings/{b['id']}/refine").json()
    wait_for(lambda: next(j for j in server.store.jobs if j["id"] == job["id"])["status"] == "running")
    merged = client.post("/api/meetings/merge", json={"meeting_ids": [b["id"], a["id"]]}).json()
    assert merged["id"] == a["id"] and merged["merged_by"] == "user" and len(merged["segment_ids"]) == 2
    assert b["id"] not in server.store.meetings
    wait_for(lambda: server.store.meetings[a["id"]]["notes_status"] == "done", message="merged notes")
    stale = next(j for j in server.store.jobs if j["id"] == job["id"])
    assert stale["status"] == "done" and stale["error"].startswith("Discarded")
    assert (root / "merged" / a["id"] / "notes.md").exists()
    assert client.patch(f"/api/meetings/{a['id']}", json={"title": "  Roadmap   sync "}).json()["title"] == "Roadmap sync"
    assert client.patch(f"/api/meetings/{a['id']}", json={"title": ""}).status_code == 400
    parts = client.post(f"/api/meetings/{a['id']}/split").json()
    assert [len(p["segment_ids"]) for p in parts] == [1, 1] and parts[0]["id"] == a["id"]
    assert parts[0]["notes_status"] == "done"  # single-segment notes already existed
    wait_for(lambda: server.store.meetings[parts[1]["id"]]["notes_status"] == "done", message="split notes")
    assert client.post(f"/api/meetings/{a['id']}/split").status_code == 400
    assert client.post(f"/api/meetings/{empty['id']}/refine").status_code == 409
    page = client.get("/api/segments", params={"before": "2026-11-24_12-00-00", "limit": 1}).json()
    assert [s["id"] for s in page["segments"]] == ["2026-11-24_11-00-00"]
    assert [m["id"] for m in page["meetings"]] == [parts[1]["id"]]


def test_delete_lines_marks_notes_stale_and_regenerate_clears_it(serve, tmp_path):
    root = tmp_path / "meetings"
    write_segment(root, "2026-11-24_10-00-00", ["Keep this line.", "Remove this noise.", "Keep this too."])
    (root / "2026-11-24_10-00-00" / "notes.md").write_text("# Old notes\n")
    server, client = serve()
    meeting = client.get("/api/snapshot").json()["meetings"][0]
    assert meeting["notes_status"] == "done" and not meeting.get("notes_stale")
    segment_id = meeting["segment_ids"][0]
    target = {"segment_id": segment_id, "source": "mic", "id": "r1-u1"}
    url = f"/api/meetings/{meeting['id']}/lines/delete"
    assert client.post(url, json={"lines": []}).status_code == 400
    assert client.post(url, json={"lines": [dict(target, id="r1-u9")]}).status_code == 404
    assert client.post(url, json={"lines": [dict(target, segment_id="2026-11-24_11-00-00")]}).status_code == 400
    result = client.post(url, json={"lines": [target]}).json()
    assert result["meeting"]["notes_stale"] is True and result["segments"][0]["line_count"] == 2
    lines = client.get(f"/api/segments/{segment_id}").json()["lines"]
    assert [line["text"] for line in lines] == ["Keep this line.", "Keep this too."]
    transcript = (root / segment_id / "transcript.md").read_text()
    assert "Remove this noise." not in transcript and "Keep this too." in transcript
    assert "Remove this noise." not in client.get(f"/api/meetings/{meeting['id']}").json()["transcript_md"]
    # The journal stays append-only: the deletion is an event, not a rewrite.
    assert '"type": "delete"' in (root / segment_id / "events.jsonl").read_text()
    client.post(f"/api/meetings/{meeting['id']}/refine")
    wait_for(lambda: server.store.meetings[meeting["id"]]["notes_stale"] is False
             and server.store.meetings[meeting["id"]]["notes_status"] == "done", message="regenerated notes")
    assert "Remove this noise." not in (root / segment_id / "notes.md").read_text()


def test_delete_meeting_moves_folders_to_trash(serve, tmp_path):
    root = tmp_path / "meetings"
    write_segment(root, "2026-11-24_10-00-00", ["First part."])
    write_segment(root, "2026-11-24_11-00-00", ["Second part."])
    server, client = serve()
    a, b = sorted(client.get("/api/snapshot").json()["meetings"], key=lambda m: m["started_at"])
    merged = client.post("/api/meetings/merge", json={"meeting_ids": [a["id"], b["id"]]}).json()
    wait_for(lambda: server.store.meetings[merged["id"]]["notes_status"] == "done", message="merged notes")
    assert client.delete("/api/meetings/nope").status_code == 404
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "snapshot"
        result = client.delete(f"/api/meetings/{merged['id']}").json()
        seen = set()
        while {"segment_removed", "meeting_removed"} - seen:
            seen.add(socket.receive_json()["type"])
    trash = Path(result["trash"])
    assert sorted(p.name for p in trash.iterdir()) == ["2026-11-24_10-00-00", "2026-11-24_11-00-00", "merged"]
    assert not (root / "2026-11-24_10-00-00").exists() and not (root / "merged" / merged["id"]).exists()
    assert server.store.meetings == {} and server.store.segments == {}
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["meetings"] == [] and snapshot["segments"] == []
    # Trash is not re-imported on the next start.
    server2, client2 = serve()
    assert client2.get("/api/snapshot").json()["meetings"] == []


def test_cannot_delete_while_recording(serve):
    server, client = serve()
    client.post("/api/recording/start", json={"engine": "apple"})
    wait_for(lambda: server.recorder.state == "recording", message="recording")
    meeting_id = server.store.segments[server.recorder.payload()["segment_id"]]["meeting_id"]
    assert client.delete(f"/api/meetings/{meeting_id}").status_code == 409
    assert client.post(f"/api/meetings/{meeting_id}/lines/delete", json={"lines": [
        {"segment_id": server.recorder.payload()["segment_id"], "source": "mic", "id": "x"}]}).status_code == 409
    client.post("/api/recording/stop")


def test_export_day_collects_notes_in_order(serve, tmp_path):
    root = tmp_path / "meetings"
    write_segment(root, "2026-11-24_14-00-00", ["Afternoon review."])
    write_segment(root, "2026-11-24_09-00-00", ["Morning standup."])
    write_segment(root, "2026-11-25_09-00-00", ["Next day."])
    (root / "2026-11-24_09-00-00" / "notes.md").write_text(
        "# Meeting\n\n## Summary\nStandup went well.\n\n```\n# not a heading\n```\n")
    server, client = serve()
    assert client.get("/api/export/day/2026-11-23").status_code == 404
    assert client.get("/api/export/day/24-11-2026").status_code == 400
    response = client.get("/api/export/day/2026-11-24")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/markdown")
    assert 'filename="voice-notes-2026-11-24.md"' in response.headers["content-disposition"]
    text = response.text
    assert text.startswith("# Meeting notes: Tuesday, November 24, 2026\n")
    assert "2 meetings" in text and "Next day" not in text
    assert text.index("## Meeting 09:00") < text.index("## Meeting 14:00")
    assert "\n### Summary\nStandup went well." in text and "#### Summary" not in text
    assert "\n# Meeting\n" not in text
    assert "```\n# not a heading\n```" in text  # code blocks are left alone
    assert "*No notes yet.*" in text  # the 14:00 meeting has none


def test_websocket_snapshot_and_upserts(serve, tmp_path):
    write_segment(tmp_path / "meetings", "2026-11-24_10-00-00", ["Hello there."])
    server, client = serve()
    meeting_id = next(iter(server.store.meetings))
    with client.websocket_connect("/ws") as socket:
        snapshot = socket.receive_json()
        assert snapshot["type"] == "snapshot"
        assert {"recorder", "settings", "segments", "meetings", "speakers", "health", "jobs", "live",
                "activity"} <= set(snapshot)
        socket.send_text("pong, or anything else")
        client.patch(f"/api/meetings/{meeting_id}", json={"title": "Renamed"})
        while (message := socket.receive_json())["type"] != "meeting":
            pass
        assert message["meeting"]["title"] == "Renamed"
        client.post("/api/recording/start")
        seen = set()
        while not {"recorder", "segment", "levels", "line"} <= seen:
            seen.add(socket.receive_json()["type"])
        client.post("/api/recording/stop")


def test_rest_basics(serve):
    server, client = serve()
    assert client.get("/api/devices").json()["microphones"][0]["name"] == "Fake Microphone"
    assert client.put("/api/settings", json={"segment_minutes": 0}).status_code == 400
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 400
    settings = client.put("/api/settings", json={"auto_merge": False, "mic": ""}).json()
    assert settings["auto_merge"] is False and settings["mic"] is None
    assert json.loads((server.store.root / "state.json").read_text())["settings"]["auto_merge"] is False
    assert client.post("/api/recording/start", json={"engine": "nope"}).status_code == 400
    assert client.get("/api/health").json()["server_rss_mb"] > 0
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/speakers").json() == []
    assert client.get("/api/nope").status_code == 404
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]


def test_failed_start_does_not_leave_recording_desired(serve):
    server, client = serve()

    async def broken(settings):
        raise RuntimeError("Cannot build the capture helper")
    server.helper_command = broken
    response = client.post("/api/recording/start")
    assert response.status_code == 409 and "Cannot build" in response.json()["detail"]
    assert server.store.recording_desired is False
    assert json.loads((server.store.root / "state.json").read_text())["recording_desired"] is False
    assert not server.store.segments and server.recorder.state == "idle"


def test_speaker_endpoints(serve, tmp_path):
    identifier = "3f2a91c4-0000-4000-8000-000000000001"
    folder = tmp_path / "speakers" / identifier
    folder.mkdir(parents=True)
    (folder / "speaker.json").write_text(json.dumps({"name": None, "created_at": "2026-11-25T09:00:00+00:00"}))
    (folder / "sample.wav").write_bytes(b"RIFF....WAVE")
    server, client = serve()
    assert client.get("/api/speakers").json()[0]["display_name"] == "3f2a91c4"
    server.recorder.live.append({"segment_id": "s", "id": "r1-u1", "source": "system", "start": 0, "end": 1,
                                 "wall": "", "text": "Hi", "speaker": "3f2a91c4",
                                 "speaker_uuid": identifier})
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "snapshot"
        named = client.post(f"/api/speakers/{identifier}/name", json={"name": "Priya"}).json()
        assert named[0]["display_name"] == "Priya"
        # Lines already on screen are relabelled, not only future ones.
        message = wait_for(lambda: (m := socket.receive_json())["type"] == "line" and m, message="relabelled line")
        assert message["line"]["speaker"] == "Priya" and server.recorder.live[0]["speaker"] == "Priya"
    assert client.get(f"/api/speakers/{identifier}/sample.wav").headers["content-type"] == "audio/wav"
    assert client.post("/api/speakers/merge", json={"source": identifier, "target": identifier}).status_code == 400
    assert client.delete(f"/api/speakers/{identifier}").json() == []


# Units


class FakeSocket:
    async def send_text(self, text):
        pass

    async def close(self, code=1000):
        pass


def test_client_queue_is_bounded_and_drops_levels_first():
    client = Client(FakeSocket(), limit=5, stuck_seconds=0.2)
    for _ in range(4):
        client.push("levels", "l")
    client.push("line", "a")
    assert client.push("levels", "dropped") and len(client.queue) == 5
    client.push("meeting", "m")
    assert len(client.queue) == 5 and [k for k, _ in client.queue].count("levels") == 3
    for _ in range(3):
        client.push("segment", "s")
    assert all(kind not in ("levels", "partial") for kind, _ in client.queue)
    assert client.push("job", "j")  # full, but only just
    time.sleep(0.25)
    assert not client.push("job", "j")  # stuck for longer than the limit


def test_hub_disconnects_stuck_clients():
    async def scenario():
        hub = Hub(limit=2, stuck_seconds=0)
        client = Client(FakeSocket(), 2, 0)
        hub.clients.add(client)
        for _ in range(4):
            hub.broadcast("line", x=1)
        await asyncio.sleep(0)
        assert client not in hub.clients and client.closed
    asyncio.run(scenario())


def test_stuck_client_connection_ends_when_dropped():
    class StuckSocket:
        def __init__(self):
            self.closed_with, self.disconnect = None, asyncio.Event()

        async def accept(self):
            pass

        async def send_text(self, text):
            await asyncio.Event().wait()  # the peer never reads: the write never completes

        async def receive(self):
            await self.disconnect.wait()
            return {"type": "websocket.disconnect"}

        async def close(self, code=1000):
            self.closed_with = code

    async def scenario():
        hub, socket = Hub(limit=3, stuck_seconds=0), StuckSocket()
        serving = asyncio.create_task(hub.serve(socket, lambda: {}))
        await asyncio.sleep(0.05)
        for _ in range(10):
            hub.broadcast("line", x=1)
        # The handler returns (freeing the connection) even though send_text never completes.
        await asyncio.wait_for(serving, 2)
        await asyncio.sleep(0)
        assert not hub.clients and socket.closed_with == 1013 and not hub._closing
    asyncio.run(scenario())


def test_notes_jobs_coalesce_while_queued(tmp_path):
    async def scenario():
        server = web_app.Server(make_config(tmp_path))  # workers not started: jobs stay queued
        first = server.jobs.enqueue("notes", "m1")
        assert server.jobs.enqueue("notes", "m1") is first
        assert server.jobs.enqueue("notes", "m2") is not first
        first["status"] = "running"
        assert server.jobs.enqueue("notes", "m1") is not first  # a running job does not block a newer one
        server.store.flush()
        assert json.loads((server.store.root / "jobs.json").read_text())["jobs"][0]["target"] == "m1"
    asyncio.run(scenario())


def test_continuation_verdicts():
    assert fake_verdict(["We discussed the budget."], ["Back to the budget numbers."])["is_continuation"]
    assert not fake_verdict(["Short talk."], ["Other words here."])["is_continuation"]
    verdict = parse_verdict('Sure: {"is_continuation": true, "confidence": 1.4, "reason": "Same topic."}')
    assert verdict == {"is_continuation": True, "confidence": 1.0, "reason": "Same topic."}
    with pytest.raises(ValueError):
        parse_verdict("no json")


def test_refine_worker_accepts_system_prompt(monkeypatch):
    import claude_agent_sdk as sdk
    from voice_notes import refine

    seen = []

    async def query(*, prompt, options):
        seen.append((prompt, options.system_prompt))
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                session_id="test", result='{"is_continuation": false}')

    monkeypatch.setattr(sdk, "query", query)
    asyncio.run(refine._sdk_ask("FIRST and SECOND", "claude-haiku-5-5", "Judge continuity."))
    assert seen == [("FIRST and SECOND", "Judge continuity.")]


def test_service_plist(tmp_path):
    from voice_notes.web.service import LABEL, build_plist
    plist = build_plist(port=9000, data_dir=tmp_path / "m", working_dir=tmp_path, build_dir=tmp_path / ".build",
                        speakers_dir=tmp_path / "s")
    assert plist["Label"] == LABEL and plist["KeepAlive"] and plist["RunAtLoad"]
    arguments = plist["ProgramArguments"]
    assert arguments[:3] == [sys.executable, "-m", "voice_notes.cli"] and "serve" in arguments
    assert arguments[arguments.index("--port") + 1] == "9000"
    assert plist["WorkingDirectory"] == str(tmp_path.resolve())


def test_serve_refuses_remote_host(tmp_path):
    from voice_notes import cli
    assert cli.main(["serve", "--host", "0.0.0.0", "--data-dir", str(tmp_path)]) == 1


def test_index_saves_are_coalesced_inside_the_event_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "FLUSH_DELAY", 0.05)
    store = Store(tmp_path)
    writes = []
    original = store._write
    monkeypatch.setattr(store, "_write", lambda *args: writes.append(args[0]) or original(*args))

    async def scenario():
        for number in range(20):
            store.meetings[f"m{number}"] = {"id": f"m{number}"}
            store.save_index()
        assert writes == []  # nothing written synchronously
        await asyncio.sleep(0.3)
    asyncio.run(scenario())
    assert writes == ["index"]
    assert len(json.loads((tmp_path / "index.json").read_text())["meetings"]) == 20
