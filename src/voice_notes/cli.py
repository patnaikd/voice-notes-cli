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

from .native import build_helper
from .meter import AudioMeter
from .refine import refine
from .session import atomic_write, read_segments, render_transcript, timestamp


def format_notes(journal: Path, title: str, model: str) -> Path:
    segments = read_segments(journal)
    transcript = render_transcript(segments, title)
    atomic_write(journal.parent / "transcript.md", transcript)
    if not any(s["text"].strip() for s in segments):
        raise RuntimeError("No finalized speech was captured; raw events are preserved")
    print(f"Formatting with {model} (sending transcript text to Claude)…", file=sys.stderr)
    notes = asyncio.run(refine(transcript, model, journal.parent))
    path = journal.parent / "notes.md"
    atomic_write(path, notes)
    return path


def record(args):
    if args.speakers and args.engine != "moonshine":
        raise ValueError("--speakers requires --engine moonshine")
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
    }, indent=2) + "\n")
    print(f"Meeting files: {directory.resolve()}", file=sys.stderr)
    failures = []
    sources = ["mic", "system"] if args.source == "both" else [args.source]
    meter = AudioMeter(sources, remote_label="Speaker",
                       enabled=not args.no_meter)
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
            if event["type"] == "segment":
                meter.print(f"[{timestamp(event['start'])}] {event['source']}: {event['text']}", stream=sys.stdout)
            elif event["type"] == "partial" and args.partials:
                meter.print(f"  … {event['source']}: {event['text']}", stream=sys.stdout)
            elif event["type"] == "error":
                failures.append(event["message"])
                meter.print(event["message"])
            elif "message" in event:
                meter.print(event["message"])
            if event["type"] == "ready":
                meter.start()

        recognizer = None
        process = None
        timer = None
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
                recognizer = Moonshine(["mic", "system"] if args.source == "both" else [args.source],
                                      args.locale.split("-")[0].split("_")[0], args.speakers,
                                      args.model_cache, receive)
            for number in (signal.SIGINT, signal.SIGTERM):
                old_signals[number] = signal.signal(number, stop)
            process = subprocess.Popen([
                str(helper), "--engine", args.engine, "--source", args.source,
                "--app", args.app, "--locale", args.locale,
                "--mic", args.mic or "",
            ], stdout=subprocess.PIPE, text=True, start_new_session=True)
            duration_timer = None
            try:
                for line in process.stdout:
                    event = json.loads(line)
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
            for number, handler in old_signals.items():
                signal.signal(number, handler)
            try:
                if recognizer:
                    recognizer.close()
            finally:
                atomic_write(directory / "transcript.md", render_transcript(read_segments(journal_path), args.title))
    print(f"Transcript saved: {directory / 'transcript.md'}", file=sys.stderr)
    if failures:
        has_speech = any(segment["text"].strip() for segment in read_segments(journal_path))
        recovery = (f". Recover captured speech with: voice-notes refine '{journal_path}'"
                    if has_speech else ". No speech was captured; start a new recording after resolving the error.")
        raise RuntimeError("; ".join(failures) + recovery)
    if not args.no_refine:
        print(format_notes(journal_path, args.title, args.model))


def doctor(args):
    print(f"macOS: {platform.mac_ver()[0] or 'not macOS'}; architecture: {platform.machine()}")
    for package in ("claude-agent-sdk", "moonshine-voice"):
        try:
            print(f"{package}: {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"{package}: not installed")
    print("ANTHROPIC_API_KEY: " + ("set" if os.environ.get("ANTHROPIC_API_KEY") else "not set; Claude Code login may be usable"))
    result = subprocess.run([str(build_helper(args.build_dir)), "--doctor"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    for line in result.stdout.splitlines():
        status = json.loads(line)
        if status.get("type") == "error":
            raise RuntimeError(status["message"])
        print(json.dumps(status, indent=2))


def positive(value):
    import math
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def devices(args):
    result = subprocess.run([str(build_helper(args.build_dir)), "--devices"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    payload = json.loads(result.stdout)
    for heading, channels, default in [("Microphones", "input_channels", "default_input"),
                                        ("Output devices", "output_channels", "default_output")]:
        print(heading + ":")
        for device in payload["devices"]:
            if device[channels] > 0:
                mark = " [system default]" if device[default] else ""
                print(f"  {device['name']}{mark}\n    UID: {device['uid']}")
    print('\nSelect an input with: voice-notes record --mic "Microphone name or UID"')


def main(argv=None):
    parser = argparse.ArgumentParser(description="Transcribe a macOS meeting locally and write Haiku notes.")
    parser.add_argument("--build-dir", type=Path, default=Path(".build"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check native speech availability, dependencies, and permissions")
    sub.add_parser("build", help="Compile the native capture helper")
    sub.add_parser("devices", help="List microphones and output devices, including system defaults")
    rec = sub.add_parser("record", help="Start recording; stop and format with Ctrl-C")
    rec.add_argument("--engine", choices=["apple", "moonshine"], default="apple")
    rec.add_argument("--source", choices=["both", "mic", "system"], default="both")
    rec.add_argument("--app", default="all", help="Capture all system audio (default), or filter by app bundle ID")
    rec.add_argument("--mic", help="Input device name or UID (default: macOS default input)")
    rec.add_argument("--title", default="Meeting")
    rec.add_argument("--locale", default="en-US")
    rec.add_argument("--speakers", action="store_true", help="Experimental Moonshine remote speaker diarization")
    rec.add_argument("--partials", action="store_true", help="Also print in-progress text")
    rec.add_argument("--no-meter", action="store_true", help="Hide live microphone and speaker audio meters")
    rec.add_argument("--duration", type=positive, help="Automatically stop after this many recording seconds")
    rec.add_argument("--no-refine", action="store_true", help="Keep transcription entirely local")
    rec.add_argument("--output", type=Path, default=Path("meetings"))
    rec.add_argument("--model-cache", type=Path, default=Path(".cache/moonshine"))
    rec.add_argument("--model", default="haiku", help="Claude model alias or ID (default: haiku)")
    fmt = sub.add_parser("refine", help="Format or recover an existing meeting journal")
    fmt.add_argument("journal", type=Path)
    fmt.add_argument("--title")
    fmt.add_argument("--model", default="haiku")
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            record(args)
        elif args.command == "doctor":
            doctor(args)
        elif args.command == "build":
            print(build_helper(args.build_dir))
        elif args.command == "devices":
            devices(args)
        elif args.command == "refine":
            metadata = args.journal.parent / "meeting.json"
            title = args.title or (json.loads(metadata.read_text()).get("title", "Meeting") if metadata.exists() else "Meeting")
            print(format_notes(args.journal, title, args.model))
    except (Exception, KeyboardInterrupt) as error:
        print(f"voice-notes: {error or 'interrupted'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
