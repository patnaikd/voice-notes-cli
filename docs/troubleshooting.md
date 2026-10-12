# Troubleshooting

Run `uv run voice-notes doctor` and `uv run voice-notes devices` first. Diagnostics report credential presence, not the API key's value.

## Permissions or capture startup

If the error says `The user declined TCCs for application, window, display capture`, enable the launching terminal/app or Voice Notes Capture under **System Settings → Privacy & Security → Screen & System Audio Recording**. Enable Microphone there too. Quit and reopen the launching app before retrying. `--source mic` avoids the system-audio permission requirement.

A capture startup deadline expires after 120 seconds. Permission prompts and first-time language installation may need longer: use `--startup-timeout 300`. Recording duration starts only once the helper reports ready. Startup failures still preserve already received speech.

`doctor` microphone statuses: `0` not requested, `1` restricted, `2` denied, `3` authorized. Sandboxed tools may report different permissions from your normal terminal, so verify there.

## Microphone or speaker silence

| Meter state | Meaning | Next step |
| --- | --- | --- |
| `no audio` | No recent frames received | Check device selection, permissions, and capture status |
| `silent` | Frames arrive with very little sound | Check mute, input volume, and microphone placement |
| `CLIP` | Samples approach maximum amplitude | Lower input gain or move away from the microphone |

Select the same device under **System Settings → Sound → Input** and speak. If macOS's input meter stays flat, check mute, cable, port, and input gain before changing recognition settings. If macOS sees audio, select the device explicitly:

```sh
uv run voice-notes record --source mic --mic "Microphone name or UID" --duration 15 --no-refine --partials
```

The native converter explicitly maps the first channel of multichannel microphones to mono. This prevents silent downmixing of discrete USB microphone channel layouts. A microphone that exposes its signal on another channel needs an additional channel-selection feature; this is currently unsupported.

For speaker audio, play an audible source and use the default `--app all`. If this works but an application filter does not, macOS may attribute that app's audio to another process. Use all-system capture in that case. Use headphones to reduce duplicate remote speech on the mic track.

## Transcript location and repeated text

`Meeting files:` prints the recording directory. `events.jsonl` stores finalized speech during capture; `transcript.md` is rendered when capture stops or when you run `refine`. Lines prefixed with `…` are tentative revisions requested by `--partials`; omit that flag for quieter output. Partial text is not committed to the transcript.

## Claude authentication and formatting

The CLI loads `.env` from the current working directory. Set `ANTHROPIC_API_KEY` there or in the shell. Shell values take precedence; a stale exported key can override a newer `.env` key. An API key's presence does not prove it is valid or has model access.

The default model is `claude-haiku-5-5`. If a provider reports an unavailable model, select one supported by your account using `--model`. Expired OAuth sessions, invalid keys, exhausted credit, and provider errors are returned as failures; the transcript is retained.

Authentication status checks have a 10-second deadline. SDK import, Claude startup, and each model request share a 180-second part deadline. Ctrl-C during formatting stops the worker and retains completed checkpoints. Retry `voice-notes refine PATH/events.jsonl` after correcting credentials or network access. Matching completed parts are reused; changed or corrupted checkpoints are regenerated.

## Dependency reads or imports hang

If the SDK or tests stall while reading local dependency files, preserve the generated virtual environment and rebuild from the lockfile using a fresh cache:

```sh
mkdir -p .cache
mv .venv ".cache/venv-backup-$(date +%Y%m%d-%H%M%S)"
UV_CACHE_DIR="$(mktemp -d)" uv sync --locked --all-extras --link-mode copy
uv run pytest
```

This leaves `.env`, source code, model assets, and meetings in place. The backup is a generated environment, not a project source backup. The launcher also supports editable installations when macOS hides their `.pth` path files.

## Native build failure

Install the macOS 26 SDK and check `xcode-select -p`. Compilation has a 180-second deadline and signing has a 30-second deadline. Builds use separate temporary directories and replace the helper only after successful compilation/signing. A failed rebuild keeps any previous helper.

## Reporting a problem

Include macOS version, engine, source, selected device name, the error message, and the final `capture_stats` event's frame counts/levels. Remove meeting text and personal device UIDs before sharing diagnostics. Do not attach `.env` or credentials.
