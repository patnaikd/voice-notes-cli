# Voice Notes CLI

Record microphone and system audio, transcribe locally, and turn the transcript into Markdown notes with **Claude Agent SDK + `claude-haiku-5-5`**. Works with any meeting app or browser; no Zoom integration or audio routing driver is required.

- Browser interface for all-day recording: live meters and transcript, 30-minute segments, automatic meeting grouping, and notes generated in the background.
- Separate microphone and system tracks, with timestamps and live audio meters.
- Apple SpeechAnalyzer by default, or FluidAudio (`--engine fluid`) for named speakers.
- Local transcript journal that survives capture and formatting failures.
- Named speaker identification in real time (`--engine fluid`): unknown voices are enrolled automatically; you listen to a saved sample, name them once, and every past transcript is corrected.
- Optional Claude summaries, discussion, decisions, action items, and open questions.

## Requirements

An **Apple Silicon Mac running macOS 26 or later**, Python **3.12+**, and Xcode or Command Line Tools with the **macOS 26 SDK**. Use `xcode-select --install` if Command Line Tools are missing. [uv](https://docs.astral.sh/uv/getting-started/installation/) manages Python dependencies.

The first build fetches and compiles the pinned [FluidAudio](https://github.com/FluidInference/FluidAudio) Swift package, which needs network access and several minutes. First-time speech recognition can download language assets. The `--engine fluid` CoreML models (about 850 MB) are stored in this repository with Git LFS under `models/fluid`; install [Git LFS](https://git-lfs.com/) and run `git lfs pull` after cloning, or the helper downloads them from Hugging Face on first use instead (see [models/README.md](models/README.md)). Claude formatting requires working credentials and network access; transcription does not require Claude credentials.

## Quick start

```sh
git clone https://github.com/patnaikd/voice-notes-cli.git
cd voice-notes-cli
uv sync
uv run voice-notes build
uv run voice-notes configure        # meetings folder, speech engine, Claude model and CLI
uv run voice-notes doctor
uv run voice-notes devices

# Start with local transcription only
uv run voice-notes record --title "Weekly planning" --no-refine
```

Grant **Microphone** and **Screen & System Audio Recording** permissions to the terminal/helper when prompted. After changing permissions, quit and reopen the launching terminal or app. Use headphones to reduce remote voices being captured again by the microphone.

Speak and play meeting audio. The terminal meters look like:

```text
Mic [#######.....]  -25 dB | Speaker [#########...]  -15 dB
```

Press **Ctrl-C once** to stop capture and save `transcript.md`. Finalized text is journaled during recording; `transcript.md` is rendered when capture stops. Only finalized sentences are printed unless `--partials` is set.

## Settings (`voice-notes configure`)

`voice-notes configure` asks for each setting, with the current value in brackets (Enter keeps it, `-` goes back to the default), and saves `~/.voice-notes-config.json`. Every `voice-notes` command reads that file each time it starts.

| Setting | Meaning | Default | Flag / environment |
| --- | --- | --- | --- |
| `meetings_dir` | Folder for recordings, transcripts and notes | `meetings` in the folder you start from | `record --output`, `serve --data-dir`, `speakers --meetings` / `VOICE_NOTES_MEETINGS` |
| `engine` | Speech recognition: `apple` (SpeechAnalyzer) or `fluid` (Parakeet, English, named speakers) | `record`: apple; web: fluid | `--engine` / `VOICE_NOTES_ENGINE` |
| `locale` | Speech language for the apple engine | `en-US` | `--locale` / `VOICE_NOTES_LOCALE` |
| `source` | `both`, `mic` or `system` | `both` | `--source` / `VOICE_NOTES_SOURCE` |
| `mic` | Microphone name or UID from `voice-notes devices` | macOS default input | `--mic` / `VOICE_NOTES_MIC` |
| `model` | Claude model that writes the notes | `claude-haiku-5-5` | `--model` / `VOICE_NOTES_MODEL` |
| `claude_cli` | Claude Code CLI to use instead of the bundled one | bundled | `--claude-cli` / `VOICE_NOTES_CLAUDE_CLI` |
| `speakers_dir` | Speaker registry | `~/.voice-notes/speakers` | `--speakers-dir` / `VOICE_NOTES_SPEAKERS` |
| `port` | Web interface port | `8765` | `--port` / `VOICE_NOTES_PORT` |

A command-line flag wins over an environment variable (also read from `.env`), which wins over the config file, which wins over the default. `voice-notes configure --show` prints each value and where it came from. For scripts:

```sh
uv run voice-notes configure --set engine=fluid --set meetings_dir=~/Meetings --set model=claude-sonnet-5-5
uv run voice-notes configure --unset mic
```

Engine, source, microphone, locale and model are also in the web interface; changing them there updates the config file. `VOICE_NOTES_CONFIG=/path/to/file.json` uses another file.

## Launcher (`Voice Notes.app`)

```sh
uv run voice-notes launcher install      # creates ~/Applications/Voice Notes.app
```

Double-click **Voice Notes** in `~/Applications`, the Dock or Spotlight. It opens the web interface in your browser, first starting the server in the background if nothing is answering on the configured port (an installed `voice-notes service` is reused). The app reads `~/.voice-notes-config.json` on every launch, so it does not need reinstalling after `configure`; reinstall it only if you move this checkout. If the server cannot start, an alert points to `~/Library/Logs/voice-notes/launcher.log`; the server's own output is in `server.launcher.log` there. `voice-notes launcher open` does the same from a terminal, and `voice-notes launcher uninstall` removes the app. A server started by the launcher keeps running until you log out; `voice-notes service install` is the better choice for always-on recording.

## Web interface (all-day recording)

```sh
npm --prefix web ci && npm --prefix web run build   # once, and after frontend changes
uv run voice-notes serve --open                     # http://localhost:8765/
```

Click **Start** in the browser and leave it running. Recording is split into segments at every `:00` and `:30` (configurable); **Split now** cuts one early and **Stop** ends the current one. Each closed segment gets a transcript and, in the background, Claude notes. When a segment looks like a continuation of the previous meeting, Claude decides so and the segment is merged into that meeting automatically. You can merge meetings yourself, split a merged meeting back into its segments, rename, delete (folders move to `trash/` in the data directory), and regenerate notes, and name speakers (with sample playback). In a meeting's Transcript tab you can delete stray lines, such as words picked up from room noise; the notes are then marked outdated until you regenerate them. **Export notes** next to each day downloads that day's meeting notes as one Markdown file. The Health tab shows memory, restarts, jobs, and errors; the side panel collapses to a narrow rail.

The server binds to `127.0.0.1` and has no authentication; `--allow-remote` is required for any other host. Files live under `--data-dir` (default `meetings/`): one folder per segment in the same format as `record`, `merged/<meeting>/` for merged notes, and `index.json`, `jobs.json`, `state.json`, `logs/server.log`. A crashed capture helper is restarted automatically, a hung one is killed, and after a server crash open segments are closed from their journals, pending notes jobs resume, and recording resumes if it was on.

Run it at login with launchd (restarted automatically if it exits). The agent reads `~/.voice-notes-config.json` when it starts; run `service install` again after `configure` to restart it. A `--port` or `--data-dir` given to `service install` is pinned instead:

```sh
uv run voice-notes service install     # ~/Library/LaunchAgents/local.voice-notes.web.plist
uv run voice-notes service status
uv run voice-notes service uninstall
```

The first recording asks macOS for **Microphone** and **Screen & System Audio Recording** permission for the capture helper; grant both, then start recording again. Service logs are in `~/Library/Logs/voice-notes/`.

## Enable formatted notes

Copy the template and add your own [Claude Console API key](https://platform.claude.com/) to `.env`:

```sh
cp .env.example .env
# Edit .env: ANTHROPIC_API_KEY=your-key
uv run voice-notes record --title "Weekly planning"
```

The CLI loads `.env` **from the directory where you run it**, without overriding existing shell environment variables. `.env`, local recordings, and caches are ignored by Git. The default model is `claude-haiku-5-5`; `--model` selects another model available to your account.

Notes are written by the Claude Code CLI that ships inside `claude-agent-sdk`. To use a different one, such as a newer `claude` you installed yourself, pass `--claude-cli /path/to/claude` before the command (`voice-notes --claude-cli ~/.local/bin/claude serve`) or set `VOICE_NOTES_CLAUDE_CLI`, also accepted in `.env`. `voice-notes doctor` shows which CLI is in use, and `voice-notes service install` keeps the choice for the launchd agent.

On stopping, the app sends the saved transcript text to Claude and writes `notes.md`. It checks authentication before SDK startup, isolates formatting in a worker process, and gives each part a 180-second deadline. Provider errors are shown instead of a blank failure message. API usage is billed by your provider.

## Choose sources and devices

```sh
# Select a listed microphone by name or UID
uv run voice-notes record --mic "HyperX SoloCast 2" --title "Planning"

# Microphone only; no screen recording permission needed
uv run voice-notes record --source mic --duration 60 --no-refine

# System audio only
uv run voice-notes record --source system --no-refine

# Optional application filter; default is all system audio
uv run voice-notes record --app com.apple.Safari

# Allow extra time for first-time permission prompts/model installation
uv run voice-notes record --startup-timeout 300
```

The CLI microphone selection is independent of the meeting app's selection. Names match case-insensitively; use the device UID if names are ambiguous. Startup prints the selected microphone and capture scope. The displayed output device is macOS's default routing device; apps may use a different output.

All system audio includes other tabs, apps, and notifications. App filtering depends on macOS audio attribution. A browser filter captures every audible tab in that browser.

## Named speakers (`--engine fluid`)

```sh
uv run voice-notes record --engine fluid --title "Weekly planning"
```

FluidAudio runs entirely on this Mac (CoreML, Neural Engine): Parakeet streaming recognition with punctuation, Sortformer streaming diarization, and CAM++ voice embeddings. Each track (microphone and system audio) gets its own recognizer and diarizer. English only. Text appears about 3 seconds after it is spoken, and a 53-second recording is processed in a few seconds.

Every finalized sentence is matched against a **speaker registry** of voices heard before (default `~/.voice-notes/speakers`; override with `--speakers-dir` or `VOICE_NOTES_SPEAKERS`). A voice that matches nothing is enrolled at once as an unknown speaker with a UUID, and up to 60 seconds of its speech is saved as a sample. Transcripts show the voice's short ID (`3f2a91c4`) until you name it:

```sh
uv run voice-notes speakers list
uv run voice-notes speakers play 3f2a            # listen to the saved sample
uv run voice-notes speakers name 3f2a "Priya"    # also corrects past transcripts
uv run voice-notes speakers merge 9c01 3f2a      # one person enrolled twice
uv run voice-notes speakers forget 9c01          # delete a voice
```

Journals (`events.jsonl`) store only UUIDs. Naming a speaker re-renders `transcript.md` in every meeting under `--meetings` (default `meetings/`); run `voice-notes refine` on a meeting afterwards to regenerate its Claude notes with the new names. `voice-notes transcript <events.jsonl>` re-renders one transcript without Claude.

Matching uses cosine similarity of voice embeddings; `--speaker-threshold` (default 0.70) trades merging two people against splitting one. Short interjections inherit the speaker the diarizer last placed in the same slot, and Sortformer separates at most four simultaneous speakers per track. Test without a microphone by transcribing a recording: `voice-notes record --engine fluid --input-file meeting.wav` (add `--input-realtime` to feed it at live speed).

Apple recognition does not provide speaker identification in this app; transcripts label its lines "Microphone" or "Remote audio".

## Saved files and recovery

Each recording creates a directory under `meetings/`:

| File | Contents |
| --- | --- |
| `meeting.json` | Title, start time, selected engine, sources, model, and speaker registry |
| `events.jsonl` | Durable finalized speech, speaker UUIDs and revisions, statuses, and capture statistics |
| `transcript.md` | Latest finalized text, ordered by audio timestamp |
| `notes.md` | Completed Claude notes |
| `notes-part-001.md` and `.json` | Formatting checkpoint and content fingerprints |

If capture fails, saved speech is rendered before the error is reported. A crash can leave a truncated final journal line; recovery reads the earlier complete events. Empty recordings cannot be formatted.

Retry formatting without recording again:

```sh
uv run voice-notes refine meetings/YYYY-MM-DD_HH-MM-SS/events.jsonl
```

Completed parts are reused only when their text, model, formatting prompt, and saved-note fingerprint match. Changing the input or model regenerates affected parts. Delete a part's `.json` checkpoint to deliberately regenerate it. Long meetings retain separate formatted parts rather than compressing them into one potentially lossy summary.

## Privacy and limits

Audio is streamed in memory and **is not saved**, with one exception: `--engine fluid` keeps up to 60 seconds of each enrolled speaker's speech (`sample.wav`) plus a voice embedding in the speaker registry, so you can identify them later. These are biometric data about everyone you record; keep the registry private, ask participants, and delete voices with `voice-notes speakers forget`. Local transcription uses public macOS APIs or FluidAudio. Default formatting sends **transcript text** to Anthropic; `--no-refine` skips that request. Claude gets no tools, project settings, or project working directory. A minimal ScreenCaptureKit screen stream is discarded immediately; screen images are not saved or analyzed.

The app starts and stops manually. It has no automatic meeting detection, participant-name lookup, or acoustic echo cancellation. Transcription and summaries can be wrong; retain the raw transcript as the reference. Speak clearly and verify decisions, names, numbers, and action ownership before sharing notes.

## Development and documentation

```sh
uv sync --all-extras
uv run pytest
uv run voice-notes build
uv build
```

Tests exercise recovery, partial checkpoints, malformed events, capture startup deadlines, worker cleanup, native microphone conversion, and the web server (segment boundaries including DST, late-final routing, helper crash/stall restarts, crash recovery, auto-merge, merge/split, jobs, and the WebSocket) using a fake capture helper (`tests/fake_helper.py`) and a fake LLM. GitHub Actions runs tests and package builds on Linux; native conversion is also tested locally on macOS. Live microphone/system transcription and an authenticated `claude-haiku-5-5` formatting request were verified on macOS 26. Speaker identification was verified on synthetic voices and file input only: a three-voice test attributed all ten turns correctly, and real-model integration tests enroll and re-recognize voices (`VOICE_NOTES_FLUID_TEST=1 uv run pytest tests/test_fluid.py`). It has not been tested live on a microphone or with real people, and no accuracy benchmark has been performed.

- [Troubleshooting](docs/troubleshooting.md): permissions, silence, authentication, and stalled environments.
- [Architecture](docs/architecture.md): capture, event protocol, persistence, and shutdown.
- [Research](docs/research.md): Apple models, FluidAudio, and implementation decisions with sources.
