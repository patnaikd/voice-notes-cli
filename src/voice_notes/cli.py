import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .capture import helper_arguments, list_devices, parse_event, startup_timeout
from .native import build_helper
from .meter import AudioMeter
from .refine import refine
from .session import atomic_write, read_segments, render_transcript, timestamp
from .speakers import DEFAULT_DIR, SpeakerRegistry, rerender_transcripts, short_id


def format_notes(journal: Path, title: str, model: str, *, registry=None) -> Path:
    segments = read_segments(journal)
    transcript = render_transcript(segments, title, registry)
    atomic_write(journal.parent / "transcript.md", transcript)
    if not any(s["text"].strip() for s in segments):
        raise RuntimeError("No finalized speech was captured; raw events are preserved")
    print(f"Formatting with {model}…", file=sys.stderr)
    notes = asyncio.run(refine(transcript, model, journal.parent))
    path = journal.parent / "notes.md"
    atomic_write(path, notes)
    return path


def record(args):
    if args.speakers and args.engine != "moonshine":
        raise ValueError("--speakers requires --engine moonshine; --engine fluid always identifies speakers")
    input_file = getattr(args, "input_file", None)
    if args.engine == "fluid" and not args.locale.lower().startswith("en"):
        raise ValueError("--engine fluid transcribes English only; use --engine apple or moonshine for other languages")
    if input_file:
        if args.engine != "fluid":
            raise ValueError("--input-file requires --engine fluid")
        if not Path(input_file).is_file():
            raise ValueError(f"Audio file not found: {input_file}")
    registry = SpeakerRegistry(getattr(args, "speakers_dir", None))
    helper = build_helper(args.build_dir)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    directory = args.output / stamp
    # Refuse to overwrite another meeting started in the same second.
    directory.mkdir(parents=True, exist_ok=False)
    journal_path = directory / "events.jsonl"
    atomic_write(directory / "meeting.json", json.dumps({
        "title": args.title, "started_at": datetime.now().astimezone().isoformat(),
        "engine": args.engine, "source": args.source, "app": args.app,
        "speakers": args.speakers, "locale": args.locale, "model": args.model,
        "microphone": args.mic or "system default",
        **({"speakers_dir": str(Path(args.speakers_dir).resolve())} if args.engine == "fluid" else {}),
        **({"input_file": str(Path(input_file).resolve())} if input_file else {}),
    }, indent=2) + "\n")
    print(f"Meeting files: {directory.resolve()}", file=sys.stderr)
    failures = []
    sources = ["mic", "system"] if args.source == "both" else [args.source]
    meter = AudioMeter(sources, remote_label="Speaker", enabled=not args.no_meter)
    with journal_path.open("a", buffering=1) as journal:
        def receive(event):
            if event["type"] == "level":
                meter.update(event["source"], event["rms"], event["peak"])
                return
            if event["type"] == "audio":
                recognizer.audio(event)
                return
            if event["type"] != "partial":
                journal.write(json.dumps(event) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
            if event["type"] == "error":
                failures.append(event["message"])
            if event["type"] == "segment":
                meter.print(f"[{timestamp(event['start'])}] {event['source']}: {event['text']}", stream=sys.stdout)
            elif event["type"] == "partial" and args.partials:
                meter.print(f"  … {event['source']}: {event['text']}", stream=sys.stdout)
            elif event["type"] == "error":
                meter.print(event["message"])
            elif "message" in event:
                meter.print(event["message"])
            if event["type"] == "ready":
                meter.start()

        recognizer = None
        process = None
        deadline = args.startup_timeout or startup_timeout(args.engine)
        timer = None
        startup_timer = None
        ready = threading.Event()
        old_signals = {}
        stop_requested = False

        def stop(*_):
            nonlocal stop_requested
            if stop_requested:
                return
            stop_requested = True
            if process and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                # Give native recognizers time to finalize, then force exit.
                nonlocal timer
                timer = threading.Timer(20, force_stop)
                timer.daemon = True
                timer.start()

        def force_stop():
            if process and process.poll() is None:
                process.kill()

        try:
            if args.engine == "moonshine":
                from .moonshine import Moonshine
                print("Loading Moonshine models (first use downloads assets)…", file=sys.stderr)
                recognizer = Moonshine(sources, args.locale.split("-")[0].split("_")[0], args.speakers,
                                       args.model_cache, receive)
            if threading.current_thread() is threading.main_thread():
                for number in (signal.SIGINT, signal.SIGTERM):
                    old_signals[number] = signal.signal(number, stop)
            command = [str(helper), *helper_arguments(
                engine=args.engine, source=args.source, app=args.app, locale=args.locale, mic=args.mic,
                speakers_dir=args.speakers_dir, speaker_threshold=args.speaker_threshold,
                input_file=input_file, input_realtime=getattr(args, "input_realtime", False))]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, start_new_session=True)

            def startup_expired():
                if not ready.is_set() and process.poll() is None:
                    message = (f"Capture did not become ready within {deadline:g} seconds. "
                               "Check macOS permissions and speech asset downloads; "
                               "increase --startup-timeout for first-time setup.")
                    failures.append(message)
                    meter.print(message)
                    stop()

            startup_timer = threading.Timer(deadline, startup_expired)
            startup_timer.daemon = True
            startup_timer.start()
            duration_timer = None
            try:
                for line in process.stdout:
                    try:
                        event = parse_event(line)
                    except ValueError as error:
                        failures.append(f"Invalid capture event: {error}. Saved speech was preserved.")
                        stop()
                        break
                    if event["type"] == "ready":
                        ready.set()
                        startup_timer.cancel()
                    if event["type"] == "ready" and args.duration:
                        duration_timer = threading.Timer(args.duration, stop)
                        duration_timer.daemon = True
                        duration_timer.start()
                    receive(event)
                code = process.wait()
                if code:
                    failures.append(f"Capture helper exited with status {code}")
            finally:
                if duration_timer:
                    duration_timer.cancel()
        finally:
            meter.close()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if timer:
                timer.cancel()
            if startup_timer:
                startup_timer.cancel()
            for number, handler in old_signals.items():
                signal.signal(number, handler)
            try:
                if recognizer:
                    recognizer.close()
            finally:
                atomic_write(directory / "transcript.md",
                             render_transcript(read_segments(journal_path), args.title, registry))
    print(f"Transcript saved: {directory / 'transcript.md'}", file=sys.stderr)
    if failures:
        has_speech = any(segment["text"].strip() for segment in read_segments(journal_path))
        recovery = (f". Recover captured speech with: voice-notes refine '{journal_path}'"
                    if has_speech else ". No speech was captured; start a new recording after resolving the error.")
        raise RuntimeError("; ".join(failures) + recovery)
    if not args.no_refine:
        print(format_notes(journal_path, args.title, args.model, registry=registry))


def doctor(args):
    print(f"macOS: {platform.mac_ver()[0] or 'not macOS'}; architecture: {platform.machine()}")
    for package in ("claude-agent-sdk", "moonshine-voice"):
        try:
            print(f"{package}: {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"{package}: not installed")
    enrolled = SpeakerRegistry(args.speakers_dir).list()
    print(f"Speaker registry: {args.speakers_dir} ({len(enrolled)} enrolled, "
          f"{sum(1 for speaker in enrolled if speaker['name']) } named)")
    print("ANTHROPIC_API_KEY: " + ("set" if os.environ.get("ANTHROPIC_API_KEY") else "not set; Claude Code login may be usable"))
    result = subprocess.run([str(build_helper(args.build_dir)), "--doctor"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    for line in result.stdout.splitlines():
        status = json.loads(line)
        if status.get("type") == "error":
            raise RuntimeError(status["message"])
        print(json.dumps(status, indent=2))


def local_time(value) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def manage_speakers(args):
    registry = SpeakerRegistry(args.speakers_dir)

    def corrected(identifiers):
        updated = rerender_transcripts(args.meetings, registry, set(identifiers))
        print(f"Corrected {len(updated)} transcript(s) in {args.meetings}. "
              "Regenerate notes with: voice-notes refine <meeting>/events.jsonl" if updated else
              f"No transcripts in {args.meetings} mention this speaker.")

    if args.action == "list":
        speakers = registry.list()
        if not speakers:
            print(f"No speakers enrolled yet in {registry.root}. Record with --engine fluid to enroll voices.")
            return
        print(f"{'ID':<9} {'Name':<24} {'Sample':>7}  First heard")
        for speaker in speakers:
            print(f"{short_id(speaker['id']):<9} {(speaker['name'] or '(unnamed)')[:24]:<24} "
                  f"{speaker['sample_seconds']:>6.0f}s  {local_time(speaker['created_at'])}")
        print(f"\nListen: voice-notes speakers play <ID>    Name: voice-notes speakers name <ID> \"Name\"")
    elif args.action == "play":
        identifier = registry.find(args.speaker)
        sample = registry.sample_path(identifier)
        if not sample.is_file():
            raise RuntimeError(f"Speaker {short_id(identifier)} has no saved voice sample")
        print(f"Playing {registry.display_name(identifier)} ({short_id(identifier)})…")
        subprocess.run(["afplay", str(sample)], check=True)
    elif args.action == "name":
        identifier = registry.set_name(registry.find(args.speaker), args.name)
        print(f"{short_id(identifier)} is now {registry.display_name(identifier)}.")
        corrected([identifier])
    elif args.action == "merge":
        source, target = registry.find(args.source), registry.find(args.target)
        registry.merge(source, target)
        print(f"Merged {short_id(source)} into {short_id(target)} ({registry.display_name(target)}).")
        corrected([target])
    elif args.action == "forget":
        identifier = registry.find(args.speaker)
        label = f"{registry.display_name(identifier)} ({short_id(identifier)})"
        if not args.yes:
            if not sys.stdin.isatty():
                raise RuntimeError("Refusing to delete voice data without --yes")
            if input(f"Permanently delete the voice data for {label}? [y/N] ").strip().lower() != "y":
                print("Nothing deleted.")
                return
        registry.forget(identifier)
        print(f"Deleted {label}. Past transcripts will show this voice as an unknown speaker.")
        corrected([identifier])


def positive(value):
    import math
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def env_float(name):
    value = os.environ.get(name)
    return float(value) if value else None


def devices(args):
    payload = list_devices([str(build_helper(args.build_dir))])
    for heading, key in [("Microphones", "microphones"), ("Output devices", "outputs")]:
        print(heading + ":")
        for device in payload[key]:
            mark = " [system default]" if device["default"] else ""
            print(f"  {device['name']}{mark}\n    UID: {device['uid']}")
    print('\nSelect an input with: voice-notes record --mic "Microphone name or UID"')


def main(argv=None):
    parser = argparse.ArgumentParser(description="Transcribe a macOS meeting locally and write Haiku notes.")
    parser.add_argument("--build-dir", type=Path, default=Path(".build"))
    parser.add_argument("--speakers-dir", type=Path, default=DEFAULT_DIR,
                        help=f"Speaker registry (default: {DEFAULT_DIR}; env VOICE_NOTES_SPEAKERS)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check native speech availability, dependencies, and permissions")
    sub.add_parser("build", help="Compile the native capture helper")
    sub.add_parser("devices", help="List microphones and output devices, including system defaults")
    rec = sub.add_parser("record", help="Start recording; stop and format with Ctrl-C")
    rec.add_argument("--engine", choices=["apple", "moonshine", "fluid"], default="apple",
                     help="apple: Apple SpeechAnalyzer; moonshine: Moonshine; "
                          "fluid: FluidAudio streaming recognition with named speaker identification (English)")
    rec.add_argument("--speaker-threshold", type=float, default=0.70,
                     help="Voice similarity (0-1) needed to match a known speaker with --engine fluid (default: 0.70)")
    rec.add_argument("--input-file", type=Path, help="Transcribe an audio file instead of recording (--engine fluid)")
    rec.add_argument("--input-realtime", action="store_true", help="Feed --input-file at live speed to test latency")
    rec.add_argument("--source", choices=["both", "mic", "system"], default="both")
    rec.add_argument("--app", default="all", help="Capture all system audio (default), or filter by app bundle ID")
    rec.add_argument("--mic", help="Input device name or UID (default: macOS default input)")
    rec.add_argument("--title", default="Meeting")
    rec.add_argument("--locale", default="en-US")
    rec.add_argument("--speakers", action="store_true", help="Experimental Moonshine remote speaker diarization")
    rec.add_argument("--partials", action="store_true", help="Also print in-progress text")
    rec.add_argument("--no-meter", action="store_true", help="Hide live microphone and speaker audio meters")
    rec.add_argument("--duration", type=positive, help="Automatically stop after this many recording seconds")
    rec.add_argument("--startup-timeout", type=positive, default=None,
                     help="Seconds to wait for capture readiness (default: 120; 600 for --engine fluid)")
    rec.add_argument("--no-refine", action="store_true", help="Keep transcription entirely local")
    rec.add_argument("--output", type=Path, default=Path("meetings"))
    rec.add_argument("--model-cache", type=Path, default=Path(".cache/moonshine"))
    rec.add_argument("--model", default="claude-haiku-5-5", help="Claude model alias or ID (default: claude-haiku-5-5)")
    fmt = sub.add_parser("refine", help="Format or recover an existing meeting journal")
    fmt.add_argument("journal", type=Path)
    fmt.add_argument("--title")
    fmt.add_argument("--model", default="claude-haiku-5-5")
    render = sub.add_parser("transcript", help="Re-render transcript.md from a journal using current speaker names")
    render.add_argument("journal", type=Path)
    render.add_argument("--title")
    speakers = sub.add_parser("speakers", help="List, listen to, name, merge, or forget enrolled speakers")
    speakers.add_argument("--meetings", type=Path, default=Path("meetings"),
                          help="Meetings folder whose transcripts are corrected when a speaker changes (default: meetings)")
    actions = speakers.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="Show enrolled speakers")
    for action, text in (("play", "Play a speaker's saved voice sample"),
                         ("name", "Name a speaker and correct past transcripts"),
                         ("forget", "Delete a speaker's voice data")):
        item = actions.add_parser(action, help=text)
        item.add_argument("speaker", help="UUID, unique UUID prefix (4+ characters), or name")
        if action == "name":
            item.add_argument("name")
        if action == "forget":
            item.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    merge = actions.add_parser("merge", help="Merge a duplicate speaker into another")
    merge.add_argument("source", help="Speaker to remove")
    merge.add_argument("target", help="Speaker to keep")
    web = sub.add_parser("serve", help="Run the browser interface (all-day recording, segments, async notes)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--data-dir", type=Path, default=Path("meetings"))
    web.add_argument("--open", action="store_true", help="Open the interface in the default browser")
    web.add_argument("--allow-remote", action="store_true",
                     help="Allow a non-loopback --host (there is no authentication)")
    hidden = argparse.SUPPRESS
    web.add_argument("--segment-seconds", type=positive, default=env_float("VOICE_NOTES_SEGMENT_SECONDS"), help=hidden)
    web.add_argument("--grace-seconds", type=float, default=env_float("VOICE_NOTES_GRACE_SECONDS"), help=hidden)
    web.add_argument("--stall-seconds", type=positive, default=env_float("VOICE_NOTES_STALL_SECONDS"), help=hidden)
    web.add_argument("--helper-command", default=os.environ.get("VOICE_NOTES_HELPER_COMMAND") or None, help=hidden)
    web.add_argument("--fake-llm", action="store_true", default=os.environ.get("VOICE_NOTES_FAKE_LLM") == "1",
                     help=hidden)
    svc = sub.add_parser("service", help="Install, remove, or inspect the launchd agent that runs serve")
    svc.add_argument("action", choices=["install", "uninstall", "status"])
    svc.add_argument("--port", type=int, default=8765)
    svc.add_argument("--data-dir", type=Path, default=Path("meetings"))
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        if args.command == "record":
            record(args)
        elif args.command == "serve":
            from .web.app import serve
            return serve(args)
        elif args.command == "service":
            from .web import service
            return service.main(args)
        elif args.command == "doctor":
            doctor(args)
        elif args.command == "speakers":
            manage_speakers(args)
        elif args.command == "transcript":
            registry = SpeakerRegistry(args.speakers_dir)
            metadata = args.journal.parent / "meeting.json"
            title = args.title or (json.loads(metadata.read_text()).get("title", "Meeting") if metadata.exists() else "Meeting")
            path = args.journal.parent / "transcript.md"
            atomic_write(path, render_transcript(read_segments(args.journal), title, registry))
            print(path)
        elif args.command == "build":
            print(build_helper(args.build_dir))
        elif args.command == "devices":
            devices(args)
        elif args.command == "refine":
            metadata = args.journal.parent / "meeting.json"
            title = args.title or (json.loads(metadata.read_text()).get("title", "Meeting") if metadata.exists() else "Meeting")
            print(format_notes(args.journal, title, args.model, registry=SpeakerRegistry(args.speakers_dir)))
    except KeyboardInterrupt:
        print("voice-notes: Interrupted. Saved transcripts and formatting checkpoints are preserved.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"voice-notes: {error or 'interrupted'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
