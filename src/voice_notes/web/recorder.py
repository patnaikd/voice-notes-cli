"""Roll one continuous helper run into wall-clock segments and route events into them."""
import asyncio
import json
import logging
import os
import queue
import threading
import time
from collections import OrderedDict, deque
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path

from ..capture import startup_timeout
from ..session import atomic_write
from .store import iso, line_payload
from .supervisor import Supervisor

log = logging.getLogger("voice_notes.web")
CLOSED_IDS = 5000  # remembered line ids of closed segments, so late revisions are not duplicated


def local_midnight(day: date) -> float:
    # mktime resolves the local offset for that instant, so DST days work.
    return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))


def next_boundary(now: float, seconds: float) -> float:
    """First boundary after `now`: a multiple of `seconds` of elapsed time since local midnight.
    Elapsed (not wall) time keeps boundaries monotonic across DST changes; they are capped at the
    next midnight so each day starts on a boundary."""
    today = datetime.fromtimestamp(now).date()
    midnight = local_midnight(today)
    boundary = midnight + ((now - midnight) // seconds + 1) * seconds
    return min(boundary, local_midnight(today + timedelta(days=1)))


class OpenSegment:
    def __init__(self, record: dict, start: float, journal):
        self.record, self.start, self.journal = record, start, journal
        self.end: float | None = None
        self.line_ids: set[str] = set()
        self.close_task: asyncio.Task | None = None
        self.closed = False

    @property
    def id(self):
        return self.record["id"]


class AudioFeeder:
    """Feed Moonshine from a worker thread so decoding never blocks the event loop. The queue is bounded;
    when decoding falls behind, the oldest chunk is dropped (Timeline fills the gap with silence)."""

    def __init__(self, recognizer, on_error, limit: int = 300):
        self.recognizer, self.on_error = recognizer, on_error
        self.queue: queue.Queue = queue.Queue(maxsize=limit)
        self.dropped = 0
        self.failed = False
        self.thread = threading.Thread(target=self._run, name="moonshine-audio", daemon=True)
        self.thread.start()

    def put(self, event: dict):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            with suppress(queue.Empty):
                self.queue.get_nowait()
            self.dropped += 1
            with suppress(queue.Full):
                self.queue.put_nowait(event)

    def _run(self):
        while (event := self.queue.get()) is not None:
            try:
                self.recognizer.audio(event)
            except Exception as error:
                if not self.failed:  # report once; a broken stream would otherwise flood the log
                    self.failed = True
                    self.on_error(error)

    def close(self):
        """Drain queued audio, then finalize the recognizer. Blocking: run it in a thread."""
        self.queue.put(None)
        self.thread.join()
        self.recognizer.close()


class Recorder:
    def __init__(self, server):
        self.server = server
        config = server.config
        self.state, self.message = "idle", None
        self.open: list[OpenSegment] = []  # ordered by start; the last one is current
        self.ids: dict[tuple[str, str], OpenSegment] = {}
        self.closed_ids: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.live: deque[dict] = deque(maxlen=300)
        self.levels: dict = {}
        self.partials: dict[str, dict] = {}
        self.last_level: float | None = None
        self.last_speech = time.monotonic()
        self.next_boundary: float | None = None
        self.memory_restart = False
        self.dirty: set[str] = set()
        self.recognizer = None
        self._lock = asyncio.Lock()
        self._boundary_task: asyncio.Task | None = None
        self.close_tasks: set[asyncio.Task] = set()
        self.supervisor = Supervisor(self._on_event, self._on_state, stall_seconds=config.stall_seconds,
                                     kill_timeout=config.kill_timeout)

    # Public state

    @property
    def current(self) -> OpenSegment | None:
        return self.open[-1] if self.open else None

    @property
    def active(self) -> bool:
        return self.supervisor.active

    def segment_seconds(self) -> float:
        return self.server.config.segment_seconds or self.server.store.settings["segment_minutes"] * 60

    def grace(self) -> float:
        configured = self.server.config.grace_seconds
        return configured if configured is not None else min(20.0, self.segment_seconds() / 2)

    def payload(self) -> dict:
        settings, current = self.server.store.settings, self.current
        return {"state": self.state, "segment_id": current.id if current and self.active else None,
                "segment_started_at": current.record["started_at"] if current and self.active else None,
                "next_boundary_at": iso(self.next_boundary) if self.next_boundary and self.active else None,
                "engine": settings["engine"], "source": settings["source"], "mic": settings["mic"],
                "helper_run": self.supervisor.run, "helper_restarts": self.supervisor.restarts,
                "message": self.message}

    def live_payload(self) -> dict:
        current = self.current
        return {"segment_id": current.id if current else None, "lines": list(self.live)}

    def _set_state(self, state: str, message: str | None = None):
        self.state, self.message = state, message
        self.server.hub.broadcast("recorder", recorder=self.payload())

    # Commands

    async def start(self, changes: dict | None = None, resumed=False):
        async with self._lock:
            if self.active:
                raise RuntimeError("Recording is already running")
            store = self.server.store
            if changes:
                store.settings = self.server.validate_settings(changes)
                self.server.hub.broadcast("settings", settings=store.settings)
            store.recording_desired = True
            store.save_state()
            try:
                command = await self.server.helper_command(store.settings)
                if store.settings["engine"] == "moonshine":
                    await self._load_moonshine()
            except BaseException:
                if not resumed:  # a failed manual start must not auto-start after the next server restart
                    store.recording_desired = False
                    store.save_state()
                raise
            self.supervisor.startup_timeout = self.server.config.startup_timeout or startup_timeout(
                store.settings["engine"])
            self._open_segment(time.time())
            self._boundary_task = self.server.spawn(self._boundaries(), "segment boundaries")
            self.supervisor.start(command)
            self._set_state("starting", "Starting capture helper…")
            self.server.activity("warning" if resumed else "info",
                                 "Recording resumed after a restart" if resumed else "Recording started")
            return self.payload()

    async def stop(self, *, keep_desired=False, reason="Recording stopped"):
        """Finalize and close every open segment; shutdown keeps `recording_desired` for a restart."""
        async with self._lock:
            if not keep_desired:
                self.server.store.recording_desired = False
                self.server.store.save_state()
            if not self.active and not self.open:
                return self.payload()
            self._set_state("stopping", "Finalizing speech…")
            if self._boundary_task:
                self._boundary_task.cancel()
                self._boundary_task = None
            await self.supervisor.stop(self.server.config.stop_grace)
            if self.recognizer:
                await asyncio.to_thread(self.recognizer.close)
                self.recognizer = None
            for segment in list(self.open):
                await self._close(segment, segment.end or time.time())
            await asyncio.gather(*self.close_tasks, return_exceptions=True)
            self.next_boundary, self.levels, self.partials = None, {}, {}
            self._set_state("idle", None)
            self.server.activity("info", reason)
            return self.payload()

    async def split(self):
        async with self._lock:
            if not self.active or not self.open:
                raise RuntimeError("Not recording")
            self._roll(time.time())
            return self.payload()

    # Segments

    def _open_segment(self, start: float):
        store, settings = self.server.store, self.server.store.settings
        segment_id = store.new_segment_id(start)
        directory = store.segment_dir(segment_id)
        directory.mkdir(parents=True)
        record = {"id": segment_id, "started_at": iso(start), "ended_at": None, "status": "recording",
                  "line_count": 0, "meeting_id": "", "engine": settings["engine"], "continuation": None, "error": None}
        store.segments[segment_id] = record
        meeting = store.new_meeting(record)
        atomic_write(directory / "meeting.json", json.dumps({
            "title": meeting["title"], "started_at": record["started_at"], "engine": settings["engine"],
            "source": settings["source"], "app": "all", "speakers": False, "locale": "en-US",
            "model": settings["model"], "microphone": settings["mic"] or "system default",
            **({"speakers_dir": str(self.server.registry.root.resolve())} if settings["engine"] == "fluid" else {}),
            "kind": "segment", "segment_start": record["started_at"], "segment_end": None,
        }, indent=2) + "\n")
        store.save_index()
        self.open.append(OpenSegment(record, start, (directory / "events.jsonl").open("a", buffering=1)))
        self.live.clear()
        self.server.hub.broadcast("segment", segment=record)
        self.server.hub.broadcast("meeting", meeting=meeting)
        self.server.hub.broadcast("recorder", recorder=self.payload())

    def _roll(self, at: float):
        """End the current segment at `at`, open the next, and close the old one after the grace period."""
        old = self.current
        old.end = at
        old.record.update(ended_at=iso(at), status="closing", line_count=len(old.line_ids))
        self.server.hub.broadcast("segment", segment=old.record)
        self._open_segment(at)
        old.close_task = self.server.spawn(self._close_later(old), f"close {old.id}")
        self.close_tasks.add(old.close_task)
        old.close_task.add_done_callback(self.close_tasks.discard)
        if self.memory_restart:
            self.memory_restart = False
            self.supervisor.restart("Restarting capture helper at a segment boundary to release memory")

    async def _close_later(self, segment: OpenSegment):
        await asyncio.sleep(self.grace())
        await self._close(segment, segment.end)

    async def _boundaries(self):
        while True:
            boundary = next_boundary(time.time(), self.segment_seconds())
            self.next_boundary = boundary
            self.server.hub.broadcast("recorder", recorder=self.payload())
            # Short sleeps follow the wall clock, which keeps running while the Mac sleeps.
            while (remaining := boundary - time.time()) > 0:
                await asyncio.sleep(min(remaining, 1.0))
            self._roll(boundary)

    async def _close(self, segment: OpenSegment, end: float):
        if segment.closed:
            return
        segment.closed = True
        if segment.close_task and segment.close_task is not asyncio.current_task():
            segment.close_task.cancel()
        self.open.remove(segment)
        for key in [key for key, value in self.ids.items() if value is segment]:
            del self.ids[key]
            self.closed_ids[key] = None
        while len(self.closed_ids) > CLOSED_IDS:
            self.closed_ids.popitem(last=False)
        self.dirty.discard(segment.id)
        segment.journal.close()
        store, record = self.server.store, segment.record
        record["ended_at"] = iso(end)
        meeting = store.meetings.get(record["meeting_id"])
        try:
            await asyncio.to_thread(store.finish_segment, record, meeting["title"] if meeting else "Meeting",
                                    self.server.registry)
        except Exception as error:
            record.update(status="failed", error=f"Could not render transcript: {error}")
            self.server.error(f"Segment {record['id']}: {record['error']}", error)
        if meeting:
            store.refresh_meeting(meeting)
        store.save_index()
        self.server.hub.broadcast("segment", segment=record)
        if meeting:
            self.server.hub.broadcast("meeting", meeting=meeting)
        await self.server.on_segment_closed(record["id"])

    # Helper events

    def _on_state(self, state: str, message: str):
        level = {"restarting": "warning", "error": "error"}.get(state)
        if level:
            self.server.activity(level, message)
        if state == "starting" and self.supervisor.run > 1:
            state = "restarting"
        self._set_state(state, message)

    def _on_event(self, event: dict, run: int, origin: float | None):
        kind = event["type"]
        if kind == "level":
            self.last_level = time.monotonic()
            self.levels[event.get("source", "mic")] = {"rms": event.get("rms", 0), "peak": event.get("peak", 0)}
            return
        if kind == "partial":
            self.last_speech = time.monotonic()
            self.partials[event["source"]] = {"source": event["source"], "text": event.get("text", ""),
                                              "speaker": None}
            return
        if kind == "audio":
            if self.recognizer:
                self.recognizer.put(event)
            return
        if kind == "segment":
            self._route(event, run, origin if origin is not None else time.time())
            return
        if self.current:
            self._journal(self.current, event)
        if kind in ("status", "warning", "error") and event.get("message"):
            self.server.activity({"status": "info"}.get(kind, kind), event["message"])
            if kind == "status" and self.state in ("starting", "restarting"):
                self._set_state(self.state, event["message"])
        elif kind == "speaker":
            self.server.speakers_changed()

    def _journal(self, segment: OpenSegment, event: dict):
        segment.journal.write(json.dumps(event) + "\n")
        segment.journal.flush()
        os.fsync(segment.journal.fileno())

    def _route(self, event: dict, run: int, origin: float):
        if not self.open:
            log.warning("Dropped a finalized line that arrived with no open segment")
            return
        wall = origin + event["start"]
        key = (event["source"], f"r{run}-{event['id']}")
        if key in self.closed_ids:
            # A revision (e.g. new speaker label) of a line whose segment already closed: routing it by time
            # would duplicate the line in the current segment, so it is dropped.
            log.info("Dropped a revision of %s: its segment is already closed", key[1])
            return
        target = self.ids.get(key)
        if target is None or target.closed:
            target = next((s for s in reversed(self.open) if s.start <= wall), self.open[0])
            self.ids[key] = target
        start = max(0.0, wall - target.start)
        line = dict(event, id=key[1], start=start, end=max(start, origin + event["end"] - target.start))
        self._journal(target, line)
        if line["text"].strip():
            target.line_ids.add(line["id"])
        else:
            target.line_ids.discard(line["id"])
        target.record["line_count"] = len(target.line_ids)
        self.dirty.add(target.id)
        self.last_speech = time.monotonic()
        payload = line_payload(target.id, line, target.start, self.server.registry)
        if target is self.current:
            for index, existing in enumerate(self.live):
                if existing["id"] == payload["id"]:
                    self.live[index] = payload
                    break
            else:
                self.live.append(payload)
        self.server.hub.broadcast("line", line=payload)
        self.partials[event["source"]] = {"source": event["source"], "text": "", "speaker": None}

    async def _load_moonshine(self):
        from ..moonshine import Moonshine
        settings, loop = self.server.store.settings, asyncio.get_running_loop()
        sources = ["mic", "system"] if settings["source"] == "both" else [settings["source"]]

        def emit(event):  # recognizer threads hand events back to the loop
            loop.call_soon_threadsafe(self._on_event, event, self.supervisor.run, self.supervisor.origin)

        self.server.activity("info", "Loading Moonshine models (first use downloads assets)…")
        def failed(error):
            loop.call_soon_threadsafe(self.server.error, f"Moonshine decoding failed: {error}", error)

        recognizer = await asyncio.to_thread(Moonshine, sources, "en", False, Path(".cache/moonshine"), emit)
        self.recognizer = AudioFeeder(recognizer, failed)

    def relabel_live(self):
        """After naming, merging or forgetting a speaker, resend live lines whose label changed."""
        labels: dict[str, str] = {}
        for index, line in enumerate(self.live):
            identifier = line.get("speaker_uuid")
            if not identifier:
                continue
            if identifier not in labels:
                labels[identifier] = self.server.registry.display_name(identifier)
            if labels[identifier] != line["speaker"]:
                self.live[index] = line = dict(line, speaker=labels[identifier])
                self.server.hub.broadcast("line", line=line)

    # Periodic flushes (called by the server ticker)

    def flush(self, partials: bool):
        hub = self.server.hub
        if self.levels:
            hub.broadcast("levels", levels=self.levels)
            self.levels = {}
        if partials:
            for payload in self.partials.values():
                hub.broadcast("partial", **payload)
            self.partials = {}

    def flush_segments(self):
        for segment in self.open:
            if segment.id in self.dirty:
                self.server.hub.broadcast("segment", segment=segment.record)
        self.dirty.clear()

