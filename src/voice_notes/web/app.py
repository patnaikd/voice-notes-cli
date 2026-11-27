"""FastAPI app: REST routes, the /ws push channel, startup recovery, and `voice-notes serve`."""
import asyncio
import ipaddress
import json
import logging
import logging.handlers
import shlex
import sys
import time
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from ..capture import fluid_models_dir, helper_arguments, list_devices
from ..native import build_helper
from ..speakers import SpeakerRegistry, rerender_transcripts
from .health import Health
from .hub import Hub
from .jobs import JobQueue, public
from .recorder import Recorder
from .store import OPEN, Store, epoch, iso, order, validate_settings

log = logging.getLogger("voice_notes.web")
STATIC = Path(__file__).parent / "static"
PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>Voice Notes</title>
<body style="font-family:system-ui;max-width:40em;margin:4em auto;padding:0 1em">
<h1>Voice Notes server is running</h1><p>The web interface has not been built yet. Run
<code>npm --prefix web ci &amp;&amp; npm --prefix web run build</code>, then reload. The API is at
<a href="/api/snapshot">/api/snapshot</a>.</p></body>"""


@dataclass
class Config:
    data_dir: Path = Path("meetings")
    build_dir: Path = Path(".build")
    speakers_dir: Path | None = None
    segment_seconds: float | None = None  # test override of settings.segment_minutes
    grace_seconds: float | None = None  # default: min(20 s, half a segment)
    stall_seconds: float = 60.0
    kill_timeout: float = 10.0
    stop_grace: float = 20.0
    startup_timeout: float | None = None  # default: 120 s, 600 s for fluid
    helper_command: str | None = None
    fake_llm: bool = False
    health_interval: float = 5.0
    ping_interval: float = 15.0
    server_warn_mb: float = 1024
    helper_warn_mb: float = 4096
    server_exit_mb: float = 2048
    memory_window: float = 300.0
    job_concurrency: int = 1
    job_backoff: tuple = field(default=(30, 120, 600))


class Server:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.data_dir)
        self.hub = Hub()
        self.registry = SpeakerRegistry(config.speakers_dir)
        self.activity_log: deque[dict] = deque(maxlen=100)
        self.tasks: set[asyncio.Task] = set()
        self.exit_code = 0
        self.uvicorn = None
        self.health = Health(self)
        self.jobs = JobQueue(self, concurrency=config.job_concurrency, backoff=config.job_backoff)
        self.recorder = Recorder(self)
        self._speakers_timer = None

    # Infrastructure

    def spawn(self, coro, name: str) -> asyncio.Task:
        """Run a background task whose failures are logged and reported, never fatal."""
        async def guarded():
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error(f"{name} failed: {error}", error)
        task = asyncio.create_task(guarded(), name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda _: coro.close())  # silences never-started coroutines
        return task

    def activity(self, level: str, message: str):
        entry = {"ts": iso(), "level": level, "message": message}
        self.activity_log.append(entry)
        log.log({"error": logging.ERROR, "warning": logging.WARNING}.get(level, logging.INFO), message)
        self.hub.broadcast("activity", activity=entry)

    def error(self, message: str, exc: BaseException | None = None):
        if exc is not None:
            log.debug("Traceback for: %s", message, exc_info=exc)
        self.health.record_error(message)
        self.activity("error", message)

    def request_exit(self, code: int):
        self.exit_code = code
        if self.uvicorn:
            self.uvicorn.should_exit = True

    def validate_settings(self, changes: dict) -> dict:
        return validate_settings(changes, self.store.settings)

    async def helper_command(self, settings: dict) -> list[str]:
        base = await self.helper_base()
        return base + helper_arguments(engine=settings["engine"], source=settings["source"], mic=settings["mic"],
                                       speakers_dir=self.registry.root,
                                       models_dir=fluid_models_dir() if settings["engine"] == "fluid" else None)

    async def helper_base(self) -> list[str]:
        if self.config.helper_command:
            return shlex.split(self.config.helper_command)
        return [str(await asyncio.to_thread(build_helper, self.config.build_dir))]

    # Lifecycle

    async def startup(self):
        store = self.store
        imported = await asyncio.to_thread(store.import_folders, self.registry)
        if imported:
            self.activity("info", f"Imported {len(imported)} existing recording(s) into the index")
        recovered = [s for s in store.ordered_segments() if s["status"] in OPEN]
        for segment in recovered:
            meeting = store.meetings.get(segment["meeting_id"])
            try:
                await asyncio.to_thread(store.finish_segment, segment, meeting["title"] if meeting else "Meeting",
                                        self.registry)
            except Exception as error:
                segment.update(status="failed", error=f"Recovery failed: {error}")
            if meeting:
                store.refresh_meeting(meeting)
            self.activity("warning", f"Recovered segment {segment['id']} after an unclean shutdown")
        for meeting in store.meetings.values():
            if meeting["notes_status"] == "running":
                meeting["notes_status"] = "queued"
        store.save_index()
        self.jobs.start()
        for coro, name in ((self.health.monitor(), "health monitor"), (self.health.measure_lag(), "lag monitor"),
                           (self._ticker(), "ticker"), (self._pinger(), "pinger")):
            self.spawn(coro, name)
        for segment in recovered:
            await self.on_segment_closed(segment["id"])
        if store.recording_desired:
            self.spawn(self.recorder.start(resumed=True), "resume recording")

    async def shutdown(self):
        try:
            await self.recorder.stop(keep_desired=True, reason="Server shutting down")
        finally:
            await self.jobs.close()
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.store.save_index()
            self.store.save_jobs()
            self.store.flush()
            self.store.save_state()

    async def _ticker(self):
        tick = 0
        while True:
            await asyncio.sleep(0.1)
            tick += 1
            self.recorder.flush(partials=tick % 2 == 0)
            if tick % 10 == 0:
                self.recorder.flush_segments()

    async def _pinger(self):
        while True:
            await asyncio.sleep(self.config.ping_interval)
            self.hub.broadcast("ping", t=time.time())

    # Meetings and jobs

    def update_meeting(self, meeting: dict, **fields):
        meeting.update(fields)
        self.store.save_index()
        self.hub.broadcast("meeting", meeting=meeting)

    def enqueue_notes(self, meeting: dict) -> dict | None:
        segments = [self.store.segments[i] for i in meeting["segment_ids"]]
        if any(s["status"] in OPEN for s in segments):
            return None  # queued when the last segment closes
        if not any(s["status"] == "closed" and s["line_count"] for s in segments):
            self.update_meeting(meeting, notes_status="skipped", notes_error=None)
            return None
        job = self.jobs.enqueue("notes", meeting["id"])
        self.update_meeting(meeting, notes_status="queued", notes_error=None)
        return job

    async def on_segment_closed(self, segment_id: str):
        store = self.store
        segment = store.segments[segment_id]
        meeting = store.meetings.get(segment["meeting_id"])
        if meeting is None:
            return
        previous = store.previous_segment(segment)
        gap = (epoch(segment["started_at"]) - epoch(previous["ended_at"])
               if previous and previous["ended_at"] else None)
        eligible = (segment["status"] == "closed" and store.settings["auto_merge"] and previous is not None
                    and previous["status"] in ("closed", "closing") and previous["line_count"] > 0
                    and gap is not None and gap <= 120 and len(meeting["segment_ids"]) == 1
                    and previous["meeting_id"] != meeting["id"])
        segment["continuation"] = {"status": "pending" if eligible else "skipped", "is_continuation": None,
                                   "confidence": None, "reason": None,
                                   "previous_segment_id": previous["id"] if previous else None}
        store.save_index()
        self.hub.broadcast("segment", segment=segment)
        if eligible:
            self.jobs.enqueue("continuation", segment_id)
        else:
            self.enqueue_notes(meeting)

    def merge_meetings(self, meeting_ids: list[str], merged_by: str, reason: str | None = None) -> dict:
        meeting, removed = self.store.merge(meeting_ids, merged_by, reason)
        self.store.save_index()
        for meeting_id in removed:
            self.hub.broadcast("meeting_removed", id=meeting_id)
        for segment_id in meeting["segment_ids"]:
            self.hub.broadcast("segment", segment=self.store.segments[segment_id])
        self.hub.broadcast("meeting", meeting=meeting)
        self.enqueue_notes(meeting)
        return meeting

    def split_meeting(self, meeting_id: str) -> list[dict]:
        meetings = self.store.split(meeting_id)
        self.store.save_index()
        for meeting in meetings:
            segment = self.store.segments[meeting["segment_ids"][0]]
            self.hub.broadcast("segment", segment=segment)
            if (self.store.segment_dir(segment["id"]) / "notes.md").exists():
                self.update_meeting(meeting, notes_status="done", notes_error=None)
            else:
                self.update_meeting(meeting, notes_status="none", notes_error=None)
                self.enqueue_notes(meeting)
        return meetings

    async def delete_meeting(self, meeting_id: str) -> Path:
        meeting = self.store.meetings[meeting_id]
        if any(self.store.segments[i]["status"] in OPEN for i in meeting["segment_ids"]):
            raise RuntimeError("Stop recording before deleting this meeting")
        busy = {meeting_id, *meeting["segment_ids"]}
        if any(job["status"] == "running" and job["target"] in busy for job in self.jobs.jobs):
            raise RuntimeError("Notes are being generated for this meeting; try again when they finish")
        segment_ids = list(meeting["segment_ids"])
        # Only the folder moves leave the event loop; the index, hub and activity log are loop-only.
        trash = await asyncio.to_thread(self.store.trash_folders, meeting_id, segment_ids)
        self.store.forget_meeting(meeting_id, segment_ids)
        self.store.save_index()
        # Meeting first: an open detail view then stops refetching before its segments vanish.
        self.hub.broadcast("meeting_removed", id=meeting_id)
        for segment_id in segment_ids:
            self.hub.broadcast("segment_removed", id=segment_id)
        self.activity("info", f"Deleted “{meeting['title']}” (moved to {trash.relative_to(self.store.root)})")
        return trash

    # Views

    def speakers_payload(self) -> list[dict]:
        return [{"id": s["id"], "name": s["name"], "display_name": self.registry.display_name(s["id"]),
                 "sample_seconds": s["sample_seconds"], "created_at": s["created_at"], "has_sample": s["has_sample"]}
                for s in self.registry.list()]

    def speakers_changed(self):
        if self._speakers_timer is None:  # debounce bursts of enrollment events
            def send():
                self._speakers_timer = None
                self.hub.broadcast("speakers", speakers=self.speakers_payload())
            self._speakers_timer = asyncio.get_running_loop().call_later(1.0, send)

    def snapshot(self) -> dict:
        store = self.store
        segments = store.ordered_segments()[-200:][::-1]
        meeting_ids = dict.fromkeys(s["meeting_id"] for s in segments)
        return {"recorder": self.recorder.payload(), "settings": store.settings, "segments": segments,
                "meetings": [store.meetings[i] for i in meeting_ids if i in store.meetings],
                "speakers": self.speakers_payload(), "health": self.health.payload(), "jobs": self.jobs.listing(),
                "live": self.recorder.live_payload(), "activity": list(self.activity_log)}


async def json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return body


def create_app(server: Server) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        await server.startup()
        try:
            yield
        finally:
            await server.shutdown()

    app = FastAPI(title="Voice Notes", lifespan=lifespan)
    app.state.server = server
    store = server.store

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        server.error(f"{request.method} {request.url.path} failed: {error}", error)
        return JSONResponse({"detail": str(error) or type(error).__name__}, status_code=500)

    def meeting_or_404(meeting_id: str) -> dict:
        if meeting_id not in store.meetings:
            raise HTTPException(404, "Meeting not found")
        return store.meetings[meeting_id]

    def segment_or_404(segment_id: str) -> dict:
        if segment_id not in store.segments:
            raise HTTPException(404, "Segment not found")
        return store.segments[segment_id]

    @app.get("/api/snapshot")
    async def snapshot():
        return server.snapshot()

    @app.post("/api/recording/start")
    async def start(request: Request):
        body = await json_body(request)
        unknown = set(body) - {"engine", "source", "mic"}
        if unknown:
            raise HTTPException(400, f"Unknown field(s): {', '.join(sorted(unknown))}")
        try:
            return await server.recorder.start(body)
        except ValueError as error:
            raise HTTPException(400, str(error))
        except RuntimeError as error:
            raise HTTPException(409, str(error))

    @app.post("/api/recording/stop")
    async def stop():
        return await server.recorder.stop()

    @app.post("/api/recording/split")
    async def split_now():
        try:
            return await server.recorder.split()
        except RuntimeError as error:
            raise HTTPException(409, str(error))

    @app.get("/api/devices")
    async def devices():
        try:
            return await asyncio.to_thread(list_devices, await server.helper_base())
        except Exception as error:
            raise HTTPException(503, f"Cannot list devices: {error}")

    @app.get("/api/settings")
    async def get_settings():
        return store.settings

    @app.put("/api/settings")
    async def put_settings(request: Request):
        try:
            store.settings = server.validate_settings(await json_body(request))
        except ValueError as error:
            raise HTTPException(400, str(error))
        store.save_state()
        server.hub.broadcast("settings", settings=store.settings)
        return store.settings

    @app.get("/api/segments")
    async def segments(before: str | None = None, limit: int = 50):
        ordered = store.ordered_segments()[::-1]
        if before:
            pivot = segment_or_404(before)
            ordered = [s for s in ordered if order(s) < order(pivot)]
        page = ordered[:max(1, min(limit, 500))]
        meeting_ids = dict.fromkeys(s["meeting_id"] for s in page)
        return {"segments": page, "meetings": [store.meetings[i] for i in meeting_ids if i in store.meetings]}

    @app.get("/api/segments/{segment_id}")
    async def segment_detail(segment_id: str):
        segment = segment_or_404(segment_id)
        lines = await asyncio.to_thread(store.segment_lines, segment, server.registry)
        path = store.segment_dir(segment_id) / "transcript.md"
        transcript = path.read_text() if path.exists() else ""
        return {"segment": segment, "lines": lines, "transcript_md": transcript}

    @app.get("/api/meetings/{meeting_id}")
    async def meeting_detail(meeting_id: str):
        meeting = meeting_or_404(meeting_id)
        transcript, _ = await asyncio.to_thread(store.meeting_transcript, meeting, server.registry)
        notes = store.notes_dir(meeting) / "notes.md"
        return {"meeting": meeting, "segments": [store.segments[i] for i in meeting["segment_ids"]],
                "transcript_md": transcript, "notes_md": notes.read_text() if notes.exists() else None}

    @app.patch("/api/meetings/{meeting_id}")
    async def rename(meeting_id: str, request: Request):
        meeting = meeting_or_404(meeting_id)
        title = (await json_body(request)).get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise HTTPException(400, "title must be 1 to 200 characters")
        server.update_meeting(meeting, title=" ".join(title.split()))
        return meeting

    @app.post("/api/meetings/merge")
    async def merge(request: Request):
        ids = (await json_body(request)).get("meeting_ids")
        if not isinstance(ids, list) or len(set(ids)) < 2 or not all(isinstance(i, str) for i in ids):
            raise HTTPException(400, "meeting_ids must list at least two meetings")
        for meeting_id in ids:
            meeting_or_404(meeting_id)
        return server.merge_meetings(ids, "user")

    @app.post("/api/meetings/{meeting_id}/split")
    async def split_meeting(meeting_id: str):
        meeting_or_404(meeting_id)
        try:
            return server.split_meeting(meeting_id)
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.delete("/api/meetings/{meeting_id}")
    async def delete_meeting(meeting_id: str):
        meeting_or_404(meeting_id)
        try:
            trash = await server.delete_meeting(meeting_id)
        except RuntimeError as error:
            raise HTTPException(409, str(error))
        return {"deleted": meeting_id, "trash": str(trash)}

    @app.post("/api/meetings/{meeting_id}/lines/delete")
    async def delete_lines(meeting_id: str, request: Request):
        meeting = meeting_or_404(meeting_id)
        lines = (await json_body(request)).get("lines")
        if (not isinstance(lines, list) or not lines or len(lines) > 1000
                or not all(isinstance(l, dict) and all(isinstance(l.get(k), str) for k in ("segment_id", "source", "id"))
                           for l in lines)):
            raise HTTPException(400, "lines must list {segment_id, source, id} objects")
        by_segment: dict[str, list[tuple[str, str]]] = {}
        for line in lines:
            if line["segment_id"] not in meeting["segment_ids"]:
                raise HTTPException(400, f"Segment {line['segment_id']} is not part of this meeting")
            by_segment.setdefault(line["segment_id"], []).append((line["source"], line["id"]))
        segments = [store.segments[i] for i in by_segment]
        if any(s["status"] in OPEN for s in segments):
            raise HTTPException(409, "Lines can be deleted once the segment has finished recording")
        for segment in segments:
            try:
                await asyncio.to_thread(store.delete_lines, segment, by_segment[segment["id"]], meeting["title"],
                                        server.registry)
            except KeyError as error:
                raise HTTPException(404, error.args[0])
        store.refresh_meeting(meeting)
        for segment in segments:
            server.hub.broadcast("segment", segment=segment)
        # Existing notes no longer match the transcript; the user regenerates them when ready.
        server.update_meeting(meeting, notes_stale=meeting["notes_status"] in ("done", "failed"))
        return {"meeting": meeting, "segments": [store.segments[i] for i in meeting["segment_ids"]]}

    @app.get("/api/export/day/{day}")
    async def export_day(day: str):
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "day must be YYYY-MM-DD")
        markdown = await asyncio.to_thread(store.export_day, parsed)
        if markdown is None:
            raise HTTPException(404, f"No meetings on {day}")
        return Response(markdown, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="voice-notes-{day}.md"'})

    @app.post("/api/meetings/{meeting_id}/refine")
    async def refine_meeting(meeting_id: str):
        meeting = meeting_or_404(meeting_id)
        if any(store.segments[i]["status"] in OPEN for i in meeting["segment_ids"]):
            raise HTTPException(409, "This meeting is still recording")
        job = server.enqueue_notes(meeting)
        if job is None:
            raise HTTPException(409, "This meeting has no speech to summarize")
        return public(job)

    def registry_call(action, *args):
        try:
            return action(*args)
        except ValueError as error:
            raise HTTPException(404 if "Unknown" in str(error) or "Not a speaker" in str(error) else 400, str(error))

    async def speakers_updated(identifiers: set[str]):
        await asyncio.to_thread(rerender_transcripts, store.root, server.registry, identifiers)
        server.recorder.relabel_live()
        speakers = server.speakers_payload()
        server.hub.broadcast("speakers", speakers=speakers)
        return speakers

    @app.get("/api/speakers")
    async def speakers():
        return server.speakers_payload()

    @app.post("/api/speakers/merge")
    async def merge_speakers(request: Request):
        body = await json_body(request)
        source, target = body.get("source"), body.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise HTTPException(400, "source and target are required")
        target = registry_call(server.registry.merge, source, target)
        return await speakers_updated({target})

    @app.post("/api/speakers/{speaker_id}/name")
    async def name_speaker(speaker_id: str, request: Request):
        name = (await json_body(request)).get("name")
        identifier = registry_call(server.registry.set_name, speaker_id, name if isinstance(name, str) else "")
        return await speakers_updated({identifier})

    @app.delete("/api/speakers/{speaker_id}")
    async def forget_speaker(speaker_id: str):
        registry_call(server.registry.forget, speaker_id)
        return await speakers_updated({speaker_id})

    @app.get("/api/speakers/{speaker_id}/sample.wav")
    async def speaker_sample(speaker_id: str):
        path = registry_call(server.registry.sample_path, speaker_id)
        if not path.is_file():
            raise HTTPException(404, "This speaker has no saved sample")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/health")
    async def health():
        return server.health.payload()

    @app.get("/api/jobs")
    async def jobs():
        return server.jobs.listing()

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        await server.hub.serve(socket, server.snapshot)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def api_missing(rest: str):
        raise HTTPException(404, "Not found")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        index = STATIC / "index.html"
        if not index.is_file():
            return HTMLResponse(PLACEHOLDER)
        root = STATIC.resolve()
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def setup_logging(data_dir: Path):
    directory = data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(directory / "server.log", maxBytes=5 * 1024 * 1024,
                                                   backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    for name in ("voice_notes", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
    root = logging.getLogger("voice_notes")
    root.addHandler(console)
    root.setLevel(logging.INFO)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(args) -> int:
    import uvicorn

    if not is_loopback(args.host) and not args.allow_remote:
        raise ValueError(f"Refusing to listen on {args.host}: there is no authentication. "
                         "Use --allow-remote if you really want this.")
    config = Config(data_dir=args.data_dir, build_dir=args.build_dir, speakers_dir=args.speakers_dir,
                    segment_seconds=args.segment_seconds, grace_seconds=args.grace_seconds,
                    stall_seconds=args.stall_seconds or 60.0, helper_command=args.helper_command,
                    fake_llm=args.fake_llm)
    setup_logging(config.data_dir)
    server = Server(config)
    uv = uvicorn.Server(uvicorn.Config(create_app(server), host=args.host, port=args.port, log_level="info",
                                       timeout_graceful_shutdown=5, ws_ping_interval=20, ws_ping_timeout=20))
    server.uvicorn = uv
    url = f"http://{'localhost' if is_loopback(args.host) else args.host}:{args.port}/"
    log.info("Voice Notes web interface: %s (data: %s)", url, config.data_dir.resolve())
    if args.open:
        async def open_browser():
            while not uv.started:
                await asyncio.sleep(0.1)
            webbrowser.open(url)
        original = server.startup

        async def startup():
            await original()
            server.spawn(open_browser(), "open browser")
        server.startup = startup
    uv.run()
    return server.exit_code


__all__ = ["Config", "Server", "create_app", "serve"]
