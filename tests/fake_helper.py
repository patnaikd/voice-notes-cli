"""Stand-in for the Swift capture helper: prints the same JSON lines without audio devices.

Options (flags or env): --crash-after S (FAKE_HELPER_CRASH_AFTER) exits 3 after S seconds;
--crash-once FILE (FAKE_HELPER_CRASH_ONCE) crashes only when FILE does not exist yet (it is created);
--hang-after S (FAKE_HELPER_HANG_AFTER) stops emitting anything; --ignore-term (FAKE_HELPER_IGNORE_TERM=1);
--phrases "a|b|c" (FAKE_HELPER_PHRASES); --interval S between finalized lines (FAKE_HELPER_INTERVAL);
--ready-delay S (FAKE_HELPER_READY_DELAY); --log FILE appends the received argv (FAKE_HELPER_LOG);
--emit-log FILE appends every finalized line as JSON (FAKE_HELPER_EMIT_LOG); --unique makes each line's text
unique (FAKE_HELPER_UNIQUE=1); --revise N re-emits every Nth line with a speaker change (FAKE_HELPER_REVISE).
"""
import argparse
import json
import os
import signal
import sys
import time

PHRASES = ["Let's review the quarterly budget before the launch.",
           "The budget for marketing needs another pass.",
           "I will send the revised forecast tomorrow.",
           "Can engineering confirm the release schedule?",
           "Engineering expects the release next Thursday."]
SPEAKER = "3f2a91c4-0000-4000-8000-000000000001"


def emit(**event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    env = os.environ.get
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", action="store_true")
    parser.add_argument("--crash-after", type=float, default=float(env("FAKE_HELPER_CRASH_AFTER") or 0))
    parser.add_argument("--crash-once", default=env("FAKE_HELPER_CRASH_ONCE"))
    parser.add_argument("--hang-after", type=float, default=float(env("FAKE_HELPER_HANG_AFTER") or 0))
    parser.add_argument("--ignore-term", action="store_true", default=env("FAKE_HELPER_IGNORE_TERM") == "1")
    parser.add_argument("--phrases", default=env("FAKE_HELPER_PHRASES"))
    parser.add_argument("--interval", type=float, default=float(env("FAKE_HELPER_INTERVAL") or 1.0))
    parser.add_argument("--ready-delay", type=float, default=float(env("FAKE_HELPER_READY_DELAY") or 0.1))
    parser.add_argument("--log", default=env("FAKE_HELPER_LOG"))
    parser.add_argument("--emit-log", default=env("FAKE_HELPER_EMIT_LOG"))
    parser.add_argument("--unique", action="store_true", default=env("FAKE_HELPER_UNIQUE") == "1")
    parser.add_argument("--revise", type=int, default=int(env("FAKE_HELPER_REVISE") or 0))
    args, rest = parser.parse_known_args()
    if args.log:
        with open(args.log, "a") as stream:
            stream.write(json.dumps(sys.argv[1:]) + "\n")
    if args.devices:
        emit(type="devices", devices=[
            dict(name="Fake Microphone", uid="fake-mic", input_channels=1, output_channels=0,
                 default_input=True, default_output=False),
            dict(name="Fake Speakers", uid="fake-out", input_channels=0, output_channels=2,
                 default_input=False, default_output=True)])
        return 0
    phrases = args.phrases.split("|") if args.phrases else PHRASES
    crash_after = args.crash_after
    if args.crash_once:
        crash_after = 0 if os.path.exists(args.crash_once) else (crash_after or 0.5)
        open(args.crash_once, "a").close()
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: None if args.ignore_term else stopping.append(True))
    emit(type="status", message="Fake helper loading models…")
    time.sleep(args.ready_delay)
    emit(type="ready", message="Recording both audio with fake.")
    begin = time.monotonic()
    counter, next_line, tick = 0, args.interval, 0

    emitted = open(args.emit_log, "a", buffering=1) if args.emit_log else None
    previous = None

    def line(text):
        nonlocal counter, previous
        counter += 1
        now = time.monotonic() - begin
        source = "mic" if counter % 2 else "system"
        if args.unique:
            text = f"{text} [{os.getpid()}-{counter}]"
        event = dict(type="segment", source=source, id=f"u{counter}", start=max(0.0, now - 1.2), end=now, text=text)
        if source == "system":
            event["speaker_uuid"] = SPEAKER
        if args.revise and previous and counter % args.revise == 0:
            # A speaker revision of the previous line, as the fluid engine sends seconds later.
            emit(**dict(previous, speaker_uuid=SPEAKER.replace("0001", "0002")))
        emit(**event)
        previous = event
        if emitted:
            emitted.write(json.dumps({"pid": os.getpid(), "id": event["id"], "text": text, "t": time.time()}) + "\n")

    while not stopping:
        time.sleep(0.1)
        elapsed = time.monotonic() - begin
        if crash_after and elapsed >= crash_after:
            emit(type="error", message="Fake helper crashed")
            return 3
        if args.hang_after and elapsed >= args.hang_after:
            continue
        tick += 1
        for source in ("mic", "system"):
            emit(type="level", source=source, rms=0.05 + 0.04 * (tick % 5), peak=0.3)
        if elapsed >= next_line - 0.5 and elapsed < next_line - 0.4:
            emit(type="partial", source="mic" if (counter + 1) % 2 else "system",
                 text=phrases[counter % len(phrases)].split()[0])
        if elapsed >= next_line:
            line(phrases[counter % len(phrases)])
            next_line += args.interval
    line("Final words before stopping.")
    emit(type="capture_stats", sources={"mic": {"frames": tick}, "system": {"frames": tick}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
