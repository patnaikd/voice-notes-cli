"""Textual recording dashboard; audio and durable writes stay in the backend."""
import subprocess
import signal
import threading
import time
from collections import deque
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Footer, Header, LoadingIndicator, Markdown, RichLog, Static, TabbedContent, TabPane

from .control import SessionControl
from .meter import level_db
from .session import timestamp


class SessionEvent(Message):
    def __init__(self, event):
        super().__init__()
        self.event = event


class SessionFinished(Message):
    def __init__(self, error=None):
        super().__init__()
        self.error = error


class ShutdownRequested(Message):
    def __init__(self, quit_session=True):
        super().__init__()
        self.quit_session = quit_session


class AudioLevel(Static):
    """A colored logarithmic meter with a recent peak marker."""
    def __init__(self, label, source, **kwargs):
        super().__init__(**kwargs)
        self.label, self.source = label, source
        self.level = None

    def update_level(self, rms, peak):
        self.level = (rms, peak, time.monotonic())
        self.redraw()

    def redraw(self):
        rms, peak, received = self.level or (0, 0, None)
        fresh = received is not None and time.monotonic() - received <= 1.5
        db = level_db(rms) if fresh else -60
        width = max(8, min(24, self.size.width - 20))
        count = round((db + 60) / 60 * width)
        marker = round((level_db(peak) + 60) / 60 * (width - 1)) if fresh and peak > 0 else None
        clipped = fresh and peak >= 0.99
        color = "#ff6878" if clipped else ("#54d6fa" if self.source == "mic" else "#bd98ff")
        status = "CLIP" if clipped else ("no audio" if not fresh else ("silent" if db < -55 else f"{db:4.0f} dB"))
        text = Text(f"{self.label:<9}", style="bold")
        for position in range(width):
            text.append("╸" if position == marker else "━",
                        style=color if position < count or position == marker else "#344355")
        text.append(f"  {status}", style=color if fresh else "dim")
        self.update(text)


class VoiceNotesApp(App[int]):
    TITLE = "Voice Notes"
    CSS = """
    Screen { background: #0c1422; color: #dce6f4; }
    Header { background: #19273d; }
    Footer { background: #19273d; }
    #summary { height: auto; padding: 1 2; background: #121e30; }
    #state-row { height: 1; }
    #state { width: 1fr; color: #57e0b0; text-style: bold; }
    #clock { width: 12; text-align: right; color: #9eafc5; }
    #details { height: auto; margin-top: 1; color: #9eafc5; }
    #meters { height: auto; padding: 1 2; }
    AudioLevel { height: 1; margin-bottom: 1; }
    #workspace { height: 1fr; margin: 0 1; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    RichLog { height: 1fr; background: #0c1422; padding: 0 1; }
    #partials { height: auto; max-height: 5; padding: 0 2; color: #94a5bc; }
    #saved { height: auto; max-height: 4; margin: 0 2; color: #9eafc5; }
    #actions { height: 3; margin: 1 2 0 2; }
    Button { margin-right: 1; }
    #spinner { width: 8; height: 3; }
    #notes { padding: 1 2; }
    .compact #summary { padding: 0 1; }
    .compact #details { margin-top: 0; }
    .compact #meters { padding: 0 1; }
    .compact AudioLevel { margin-bottom: 0; }
    .compact #actions { margin: 0 1; }
    """
    BINDINGS = [
        Binding("ctrl+c", "stop", "Stop / cancel notes", priority=True),
        Binding("space", "stop", "Stop & save"),
        Binding("f", "follow", "Follow transcript"),
        Binding("o", "open_files", "Open files"),
        Binding("q", "quit", "Exit", priority=True),
    ]

    def __init__(self, args, *, backend=None):
        super().__init__()
        self.args = args
        self.sub_title = args.title
        if backend is None:
            from .cli import record
            backend = record
        self.backend = backend
        self.control = SessionControl()
        self.backend_started = threading.Event()
        self.backend_done = threading.Event()
        self.phase = "starting"
        self.finished = False
        self.exit_after_save = False
        self.exit_code = 0
        self.directory = None
        self.transcript_path = None
        self.notes_path = None
        self.started = None
        self.stopped_at = None
        self.partials = {}
        self.mic_name = args.mic or "System default input"
        self.scope = "All applications" if args.app == "all" else args.app
        self.follow = True
        self.final_count = 0
        self.segments = {}
        self.diagnostics = deque(maxlen=200)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="summary"):
            with Horizontal(id="state-row"):
                yield Static("Preparing audio…", id="state", markup=False)
                yield Static("00:00:00", id="clock")
            yield Static(id="details", markup=False)
        with Vertical(id="meters"):
            if self.args.source != "system":
                yield AudioLevel("Mic", "mic", id="mic-level")
            if self.args.source != "mic":
                yield AudioLevel("Speaker", "system", id="system-level")
        with Vertical(id="workspace"):
            with TabbedContent(initial="live"):
                with TabPane("Transcript", id="live"):
                    yield RichLog(id="transcript", wrap=True, highlight=False, markup=False, max_lines=3000)
                with TabPane("Notes", id="notes-tab"):
                    intro = ("Local-only recording: Claude formatting is disabled.\n\nThe saved transcript remains available in the Transcript tab."
                             if self.args.no_refine else "Notes will appear here after recording stops.\n\nUse **Stop & format** to finalize speech and generate notes.")
                    yield Markdown(intro, id="notes")
                with TabPane("Activity", id="activity-tab"):
                    yield RichLog(id="activity", wrap=True, markup=False, max_lines=500)
        yield Static("Listening for speech…", id="partials", markup=False)
        yield Static("Files will be saved when the session starts.", id="saved", markup=False)
        with Horizontal(id="actions"):
            yield Button("Stop & save" if self.args.no_refine else "Stop & format", variant="success", id="stop")
            yield Button("Open files", id="files", disabled=True)
            yield Button("Exit", id="exit")
            yield LoadingIndicator(id="spinner")
        yield Footer()

    def on_mount(self):
        self.query_one("#meters").display = not self.args.no_meter
        self.query_one("#transcript", RichLog).border_title = "Finalized speech"
        self._details()
        self.set_interval(0.1, self._refresh)
        self._run_session()

    def on_resize(self, event):
        self.screen.set_class(event.size.height < 28, "compact")

    @work(thread=True, exit_on_error=False)
    def _run_session(self):
        self.backend_started.set()
        try:
            self.backend(self.args, observer=lambda event: self.post_message(SessionEvent(event)), control=self.control)
        except Exception as error:
            self.post_message(SessionFinished(str(error) or type(error).__name__))
        else:
            self.post_message(SessionFinished())
        finally:
            self.backend_done.set()

    def on_shutdown_requested(self, message: ShutdownRequested):
        if message.quit_session:
            self.action_quit()
        else:
            self.action_stop()

    def _details(self):
        engine = "Apple Speech" if self.args.engine == "apple" else "Moonshine"
        parts = [engine, f"Mic: {self.mic_name}" if self.args.source != "system" else "Mic off",
                 f"Speaker: {self.scope}" if self.args.source != "mic" else "Speaker off"]
        self.query_one("#details", Static).update("  •  ".join(parts))

    def _refresh(self):
        if self.started is not None:
            endpoint = self.stopped_at or time.monotonic()
            self.query_one("#clock", Static).update(timestamp(endpoint - self.started))
        for meter in self.query(AudioLevel):
            meter.redraw()

    def _set_phase(self, phase, label):
        self.phase = phase
        self.query_one("#state", Static).update(label)
        self.query_one("#spinner").display = phase in ("starting", "finalizing", "formatting")
        button = self.query_one("#stop", Button)
        button.disabled = phase in ("finalizing", "done", "failed", "canceled")
        if phase == "formatting":
            button.label = "Cancel notes"
        if phase in ("finalizing", "formatting", "done", "failed", "canceled"):
            if self.started is not None and self.stopped_at is None:
                self.stopped_at = time.monotonic()
        if phase in ("done", "failed", "canceled"):
            self.query_one("#partials", Static).update("")

    def _log(self, message, *, error=False):
        self.diagnostics.append(message)
        self.query_one("#activity", RichLog).write(Text(message, style="#ff6878" if error else "#9eafc5"))

    async def on_session_event(self, message: SessionEvent):
        event = message.event
        kind = event["type"]
        if kind == "level":
            meters = list(self.query(f"#{event['source']}-level"))
            if meters:
                meters[0].update_level(event["rms"], event["peak"])
        elif kind == "session":
            self.directory = Path(event["directory"])
            self.query_one("#saved", Static).update(f"Session: {self.directory}")
            self.query_one("#files", Button).disabled = False
        elif kind == "ready":
            self.started = time.monotonic()
            if not self.control.stop_requested.is_set():
                self._set_phase("recording", "● Recording · local transcription")
            self._log(event.get("message", "Recording started"))
        elif kind == "phase":
            phase = event["phase"]
            labels = {"starting": "Preparing audio…", "finalizing": "Finalizing speech and saving transcript…",
                      "formatting": f"Formatting notes · {self.args.model}"}
            self._set_phase(phase, labels.get(phase, phase))
        elif kind == "partial":
            self.partials[event["source"]] = event["text"].strip()
            self._partials()
        elif kind == "segment":
            key = (event["source"], event["id"])
            revised = key in self.segments
            if not revised:
                self.final_count += 1
            self.segments[key] = event
            if len(self.segments) > 1000:
                oldest = min(self.segments, key=lambda item: self.segments[item]["start"])
                del self.segments[oldest]
            log = self.query_one("#transcript", RichLog)
            if revised:
                log.clear()
                for segment in sorted(self.segments.values(), key=lambda item: item["start"]):
                    self._write_segment(segment)
            else:
                self._write_segment(event)
            self.partials.pop(event["source"], None)
            self._partials()
        elif kind == "transcript_saved":
            self.transcript_path = Path(event["path"])
            self.query_one("#saved", Static).update(f"Transcript: {self.transcript_path}")
            self._log(f"Transcript saved: {self.transcript_path}")
        elif kind == "notes_saved":
            self.notes_path = Path(event["path"])
            await self.query_one("#notes", Markdown).update(event["text"])
            self.query_one(TabbedContent).active = "notes-tab"
            self.query_one("#saved", Static).update(f"Transcript: {self.transcript_path}\nNotes: {self.notes_path}")
        elif kind == "formatting_progress":
            verb = "Reusing" if event.get("reused") else "Formatting"
            self._set_phase("formatting", f"{verb} notes · part {event['part']}/{event['total']} · {self.args.model}")
        elif kind == "status" and "device" in event:
            self.mic_name = event["device"]["name"]
            self._details()
            self._log(event.get("message", "Microphone selected"))
        elif "message" in event:
            self._log(event["message"], error=kind == "error")

    def _write_segment(self, event):
        source = event["source"]
        label = "MIC" if source == "mic" else "SPEAKER"
        speaker_ids = sorted({str(span["speaker_id"]) for span in event.get("speakers", [])
                              if span.get("speaker_id") is not None and span["speaker_id"] >= 0})
        if speaker_ids:
            label += " " + ", ".join(speaker_ids)
        line = Text()
        line.append(f"{timestamp(event['start'])}  ", style="dim")
        line.append(f"{label:<10} ", style="#54d6fa" if source == "mic" else "#bd98ff")
        line.append(event["text"].strip(), style="#dce6f4")
        self.query_one("#transcript", RichLog).write(line, scroll_end=self.follow)

    def _partials(self):
        text = "\n".join(f"{'Mic' if source == 'mic' else 'Speaker'}  … {value}"
                         for source, value in self.partials.items())
        self.query_one("#partials", Static).update(text or "Listening for speech…")

    async def on_session_finished(self, message: SessionFinished):
        canceled_setup = bool(message.error and self.control.stop_requested.is_set()
                              and message.error.startswith("Recording canceled before capture started."))
        canceled_notes = bool(message.error and self.control.skip_formatting.is_set()
                              and message.error.startswith("Formatting canceled."))
        if message.error and not (canceled_setup or canceled_notes):
            self.exit_code = 1
            self._set_phase("failed", "Session needs attention · see Activity")
            self._log(message.error, error=True)
            self.query_one(TabbedContent).active = "activity-tab"
        else:
            if canceled_setup:
                self._set_phase("canceled", "Canceled before recording started")
            else:
                label = "Saved · transcript and notes ready" if self.notes_path else "Saved · transcript ready"
                self._set_phase("done", label)
        if not self.notes_path:
            if canceled_setup:
                explanation = "Recording was canceled before capture started."
            elif canceled_notes:
                explanation = "Notes formatting was canceled. Your transcript and completed checkpoints are preserved."
            elif message.error:
                explanation = "Notes are unavailable. See **Activity** for the error and recovery guidance."
            else:
                explanation = "Transcript saved. Claude formatting was skipped for this session."
            await self.query_one("#notes", Markdown).update(explanation)
        self.finished = True
        if self.exit_after_save:
            self.exit(self.exit_code)

    def action_stop(self):
        if self.finished:
            return
        if self.phase == "formatting":
            self.control.cancel_formatting()
            self.query_one("#state", Static).update("Canceling notes · keeping saved transcript…")
        else:
            self._set_phase("finalizing", "Stopping · finalizing speech…")
            self.control.request_stop()

    def action_quit(self):
        if self.finished:
            self.exit(self.exit_code)
        else:
            self.exit_after_save = True
            self.control.request_quit()
            self.query_one("#state", Static).update("Exiting safely · saving transcript…")

    def action_follow(self):
        self.follow = not self.follow
        self.query_one("#transcript", RichLog).auto_scroll = self.follow
        self.notify("Transcript follow on" if self.follow else "Transcript follow paused")

    def action_open_files(self):
        if self.directory:
            try:
                subprocess.Popen(["open", str(self.directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as error:
                self.notify(f"Could not open folder: {error}", severity="error")

    def on_button_pressed(self, event: Button.Pressed):
        {"stop": self.action_stop, "files": self.action_open_files, "exit": self.action_quit}[event.button.id]()


def run_dashboard(args):
    app = VoiceNotesApp(args)
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.getsignal(number)
            def shutdown(received, _frame):
                app.post_message(ShutdownRequested(quit_session=received != signal.SIGINT))
            signal.signal(number, shutdown)
    try:
        result = app.run()
    finally:
        if app.backend_started.is_set() and not app.backend_done.is_set():
            app.control.request_quit()
            # Also clean up if a terminal driver or rendering error closes the UI.
            app.backend_done.wait(timeout=25)
        for number, handler in previous.items():
            signal.signal(number, handler)
    if app.transcript_path:
        print(f"Transcript saved: {app.transcript_path}")
    if app.notes_path:
        print(f"Notes saved: {app.notes_path}")
    return result if isinstance(result, int) else app.exit_code
