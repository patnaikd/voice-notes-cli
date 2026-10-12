import asyncio
from types import SimpleNamespace

from textual.widgets import TabbedContent

from voice_notes import cli
from voice_notes.session import read_segments
from voice_notes.tui import VoiceNotesApp


def arguments(tmp_path, **overrides):
    fields = dict(title="Dashboard test", engine="apple", source="both", mic="Test microphone",
                  app="all", model="claude-haiku-5-5", no_meter=False, no_refine=True,
                  speakers=False, locale="en-US", model_cache=tmp_path / "models",
                  build_dir=tmp_path / "build", output=tmp_path / "meetings",
                  partials=False, duration=None, startup_timeout=3)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def capture_helper(tmp_path):
    helper = tmp_path / "fake-capture"
    helper.write_text('''#!/bin/sh
exec python3 - <<'SCRIPT'
import json,signal,time,sys

def emit(event): print(json.dumps(event),flush=True)
def finish(*args):
    emit(dict(type="segment",source="mic",id="1",start=0,end=1,text="Final speech was saved."))
    sys.exit(0)
signal.signal(signal.SIGTERM,finish)
emit(dict(type="status",device=dict(name="Selected microphone"),message="Microphone selected"))
emit(dict(type="ready",message="Ready"))
emit(dict(type="partial",source="mic",text="Final speech"))
while True:
    emit(dict(type="level",source="mic",rms=0.1,peak=0.4))
    time.sleep(0.1)
SCRIPT
''')
    helper.chmod(0o755)
    return helper


async def wait_for(pilot, predicate):
    for _ in range(150):
        if predicate():
            return
        await pilot.pause(0.02)
    raise AssertionError("Dashboard did not reach expected state")


def test_dashboard_stop_saves_final_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "build_helper", lambda _: capture_helper(tmp_path))
    async def check():
        app = VoiceNotesApp(arguments(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(pilot, lambda: app.phase == "recording")
            assert app.mic_name == "Selected microphone"
            await wait_for(pilot, lambda: bool(app.query_one("#mic-level").level))
            await pilot.press("space")
            await wait_for(pilot, lambda: app.finished)
            assert app.exit_code == 0
            assert app.transcript_path.exists()
            assert "Final speech was saved." in app.transcript_path.read_text()
            assert len(read_segments(app.transcript_path.with_name("events.jsonl"))) == 1
            assert app.final_count == 1
            await pilot.press("q")
        assert app.return_value == 0
    asyncio.run(check())


def test_quit_during_formatting_preserves_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "build_helper", lambda _: capture_helper(tmp_path))
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(cli, "refine", stalled)
    async def check():
        app = VoiceNotesApp(arguments(tmp_path, no_refine=False))
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: app.phase == "recording")
            await pilot.press("space")
            await wait_for(pilot, lambda: app.phase == "formatting")
            await pilot.press("q")
            await wait_for(pilot, lambda: app.finished)
        assert app.return_value == 0
        assert app.transcript_path.exists()
        assert not app.transcript_path.with_name("notes.md").exists()
    asyncio.run(check())


def test_error_stays_visible_until_exit(tmp_path):
    def failure(*args, **kwargs):
        raise RuntimeError("Microphone permission denied")
    async def check():
        app = VoiceNotesApp(arguments(tmp_path), backend=failure)
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: app.finished)
            assert app.phase == "failed"
            assert "Microphone permission denied" in app.diagnostics
            assert app.query_one(TabbedContent).active == "activity-tab"
            await pilot.press("q")
        assert app.return_value == 1
    asyncio.run(check())


def test_notes_preview_revisions_and_resize(tmp_path):
    def backend(args, observer, control):
        observer(dict(type="ready"))
        observer(dict(type="segment",source="system",id="1",start=0,end=1,text="[literal] speech"))
        observer(dict(type="segment",source="system",id="1",start=0,end=1,text="Corrected speech",speakers=[dict(speaker_id=2)]))
        control.stop_requested.wait(5)
        observer(dict(type="phase",phase="formatting"))
        observer(dict(type="formatting_progress",part=1,total=1,reused=False))
        observer(dict(type="notes_saved",path=str(tmp_path / "notes.md"),text="# Summary\n\nMeeting notes."))
    async def check():
        app = VoiceNotesApp(arguments(tmp_path, no_refine=False), backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for(pilot, lambda: app.final_count == 1)
            assert app.segments[("system", "1")]["text"] == "Corrected speech"
            await pilot.resize_terminal(60, 22)
            await pilot.pause()
            assert app.screen.has_class("compact")
            assert app.query_one("#transcript").size.height >= 3
            await pilot.press("f")
            assert not app.follow
            await pilot.press("space")
            await wait_for(pilot, lambda: app.finished)
            assert app.notes_path == tmp_path / "notes.md"
            assert app.query_one(TabbedContent).active == "notes-tab"
            await pilot.press("q")
    asyncio.run(check())


def test_cli_uses_textual_in_terminal_and_plain_when_requested(monkeypatch):
    from voice_notes import tui
    seen = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(tui, "run_dashboard", lambda args: seen.append("textual") or 0)
    monkeypatch.setattr(cli, "record", lambda args: seen.append("plain"))
    assert cli.main(["record"]) == 0
    assert cli.main(["record", "--ui", "plain"]) == 0
    assert seen == ["textual", "plain"]


def test_pending_quit_does_not_start_capture(tmp_path, monkeypatch):
    def unexpected(*args):
        raise AssertionError("Capture build should not run after pending cancellation")
    monkeypatch.setattr(cli, "build_helper", unexpected)
    async def check():
        app = VoiceNotesApp(arguments(tmp_path))
        app.control.request_quit()
        async with app.run_test(size=(80,24)) as pilot:
            await wait_for(pilot, lambda: app.finished)
            assert app.phase == "canceled"
            assert not (tmp_path / "meetings").exists()
            await pilot.press("q")
        assert app.return_value == 0
    asyncio.run(check())


def test_dashboard_restores_signal_handlers(monkeypatch):
    import signal
    import threading
    from voice_notes import tui
    original = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    received = []
    done = threading.Event()
    done.set()
    class FakeApp:
        backend_started = done
        backend_done = done
        transcript_path = notes_path = None
        exit_code = 0
        def post_message(self, message):
            received.append(message.quit_session)
        def run(self):
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                signal.getsignal(number)(number, None)
            return 0
    monkeypatch.setattr(tui, "VoiceNotesApp", lambda args: FakeApp())
    assert tui.run_dashboard(None) == 0
    assert received == [True, True, False]
    assert {number: signal.getsignal(number) for number in original} == original


def test_dashboard_formats_notes_through_isolated_sdk_worker(tmp_path, monkeypatch):
    from voice_notes import refine
    monkeypatch.setattr(cli, "build_helper", lambda _: capture_helper(tmp_path))
    monkeypatch.setattr(refine, "ensure_authentication", lambda: None)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    (tmp_path / "claude_agent_sdk.py").write_text('''
class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        assert kwargs["tools"] == []
        assert kwargs["model"] == "claude-haiku-5-5"
class ResultMessage:
    is_error = False
    subtype = "success"
    result = "# Summary\\n\\nSpeech was saved and formatted."
async def query(*, prompt, options):
    assert "Final speech was saved." in prompt
    yield ResultMessage()
''')
    async def check():
        app = VoiceNotesApp(arguments(tmp_path, no_refine=False))
        async with app.run_test(size=(100,32)) as pilot:
            await wait_for(pilot, lambda: app.phase == "recording")
            await pilot.press("space")
            await wait_for(pilot, lambda: app.finished)
            assert app.exit_code == 0
            assert app.notes_path.exists()
            assert "Speech was saved and formatted." in app.notes_path.read_text()
            assert app.query_one(TabbedContent).active == "notes-tab"
            await pilot.press("q")
    asyncio.run(check())
