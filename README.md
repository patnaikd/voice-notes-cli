# Voice Notes CLI

Record microphone and system speaker audio, transcribe locally, and turn the transcript into Markdown meeting notes with **Claude Agent SDK + Haiku**.

Requires an Apple Silicon Mac running **macOS 26+**, Python 3.12+, and Xcode or Command Line Tools with the macOS 26 SDK. The native helper uses ScreenCaptureKit; no BlackHole or other audio routing driver is needed.

## Install

```sh
uv sync --extra moonshine --extra dev
uv run voice-notes build
uv run voice-notes doctor
uv run voice-notes devices
```

Or use `python3 -m venv .venv`, activate it, and `pip install -e '.[moonshine,dev]'`.

The console launcher supports editable installs even if macOS marks their `.pth` import-path file hidden. It uses the installed package's checkout metadata to find the source, so running `uv run voice-notes` does not depend on repeatedly clearing file flags.

Set `ANTHROPIC_API_KEY` in your shell for formatting. The SDK bundles its Claude Code executable. Existing supported Claude Code authentication may also work for personal use; API-key authentication is the reproducible route. Credentials are never written to meeting files.

## Record a meeting

Run this with any meeting app or browser. Microphone and all system audio are captured by default:

```sh
uv run voice-notes record --title "Weekly planning"
```

Apple's **SpeechAnalyzer** is the default local recognizer. It reuses system-managed speech assets or downloads the selected language's assets on first use. Press **Ctrl-C** to stop, finalize transcription, and generate notes with Haiku. This CLI starts and stops manually; it does not automatically detect meetings.

A live audio meter appears at the bottom of an interactive terminal for each captured source:

```text
Mic [#######.....]  -25 dB | Speaker [#########...]  -15 dB
```

The bars show incoming volume before transcription. `silent` means audio frames are arriving with very little sound; `no audio` means no frames have arrived recently; `CLIP` indicates a peak near maximum volume. Meters work with both Apple and Moonshine. Use `--no-meter` to hide them. They automatically stay off when stderr is redirected or the terminal cannot redraw lines, and meter events are not saved in the transcript journal.

Grant microphone and Screen & System Audio Recording permissions to the terminal/helper when macOS requests them. In System Settings → Privacy & Security, check both permissions and restart your terminal after changing them. `doctor` reports the current helper's permission status; microphone status `3` means authorized, `2` denied, `0` not requested, and `1` restricted.

If macOS reports `The user declined TCCs for application, window, display capture`, system-audio capture has not been authorized. Enable the terminal/app that launched this command (or Voice Notes Capture, if listed) under **Screen & System Audio Recording**, quit and reopen it, and start a new recording. A journal containing only that error has no transcript to recover. `--source mic` can record microphone audio while system-audio permission is unavailable.

Use headphones: without acoustic echo cancellation, remote voices played through speakers can also appear on the microphone track. `Microphone` is a source label, not proof of a person's identity.

Recording startup prints the selected microphone's name, UID, sample rate, and channel count, plus the application capture scope and macOS's default output device. The CLI's microphone selection is independent of your meeting app's setting. Use `voice-notes devices` to list available devices and choose your microphone:

```sh
uv run voice-notes record --mic "HyperX SoloCast 2" --title "Weekly planning"
```

Names must match a listed input (case-insensitive); use its UID if names are ambiguous. A default output device is the macOS routing default; individual apps may use another device. All system audio is captured by default, including other apps, browser tabs, and notification sounds.

```sh
# Moonshine's streaming models, with experimental remote speaker labels
uv run voice-notes record --engine moonshine --speakers --title "Design review"

# Entirely local transcription, with no Claude request
uv run voice-notes record --no-refine

# Optionally limit speaker capture to one app
uv run voice-notes record --app com.apple.Safari

# Microphone only; does not require screen recording permission
uv run voice-notes record --source mic --duration 60 --no-refine

# Display tentative text as well as finalized utterances
uv run voice-notes record --partials

# Choose a language and a specific Claude model, if needed
uv run voice-notes record --locale en-US --model haiku
```

No meeting app is required. `--app all` is the default; optional `--app BUNDLE_ID` filtering depends on macOS audio attribution. A browser filter captures other audio from that browser too. A minimal screen output is registered and immediately discarded; no screen images are saved or analyzed. Microphone audio is captured separately through AVCaptureSession, bound to the selected device, for both single-source and combined recording. Multichannel inputs use their first channel for mono speech conversion, avoiding silent automatic downmixing on USB microphones. The final journal includes per-source frame counts and maximum RMS to help distinguish missing audio from silence.

## Output and recovery

Each recording creates `meetings/YYYY-MM-DD_HH-MM-SS/`:

- `meeting.json`: title, configuration, and start time.
- `events.jsonl`: flushed event journal containing finalized transcript and speaker revisions.
- `transcript.md`: source labels and timestamps, ordered by audio time.
- `notes.md`: Haiku's summary, discussion, decisions, actions, and open questions.
- `notes-part-NNN.md`: completed formatting checkpoints; long meetings retain all parts.

Audio is streamed in memory and is **not saved**. Transcription stays local; default formatting sends transcript text to Anthropic. `--no-refine` skips that step. Claude gets no tools or project settings. Model edits can still be wrong; keep the raw transcript as the reference.

If formatting fails, the transcript remains available. Retry:

```sh
uv run voice-notes refine meetings/2026-10-10_10-30-00/events.jsonl
```

The latest speaker revision replaces the earlier label when rendering. Moonshine speaker IDs are anonymous, scoped to the remote stream, and may change during the recent clustering window. A line with several speakers lists those IDs together; detailed spans are retained in the journal. The CLI does not map voices to meeting participant names.

First-time Moonshine setup downloads models to `.cache/moonshine`. Later use can reuse the cache. The installed version's language catalog determines availability and licensing; do not assume every model has the same license.

## Checks and current limits

```sh
uv run pytest
uv run voice-notes doctor
```

The native helper compiles against the macOS 26 SDK. Unit tests cover journal recovery, speaker revisions, timestamp gaps, bounded formatting, and Claude/capture failure handling. A three-second microphone capture and clean shutdown passed on this Mac. Moonshine transcribed a bundled sample and produced speaker spans. A live meeting recording still needs screen/system audio permission and a live audio check. No ASR accuracy benchmark, live meeting validation, or authenticated Haiku request has been completed here.

See [the research and architecture notes](docs/research.md) for the built-in Apple models, Moonshine, older macOS alternatives, and primary-source links.
