"""User settings in ~/.voice-notes-config.json, read on every launch.

Each setting is resolved in this order, first match wins:

    1. command-line flag            (--output, --data-dir, --model, ...)
    2. environment variable         (VOICE_NOTES_MODEL, ...; also read from ./.env)
    3. ~/.voice-notes-config.json   (written by `voice-notes configure`)
    4. built-in default

VOICE_NOTES_CONFIG points at a different file, which tests and second setups use.
"""
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .session import atomic_write

CONFIG_ENV = "VOICE_NOTES_CONFIG"


@dataclass(frozen=True)
class Option:
    key: str
    env: str
    default: object
    label: str
    help: str
    choices: tuple = ()
    kind: str = "text"  # text, path, executable, port


OPTIONS = (
    Option("meetings_dir", "VOICE_NOTES_MEETINGS", "meetings", "Meetings folder",
           "Where recordings, transcripts and notes are saved. A relative path is relative to the "
           "folder you start voice-notes from.", kind="path"),
    Option("engine", "VOICE_NOTES_ENGINE", None, "Speech recognition",
           "apple: Apple SpeechAnalyzer, many languages. fluid: FluidAudio Parakeet on device, "
           "English only, with named speakers. Unset: `record` uses apple and the web interface uses fluid.",
           choices=("apple", "fluid")),
    Option("locale", "VOICE_NOTES_LOCALE", "en-US", "Speech language",
           "Locale for Apple speech recognition, such as en-US, en-GB or de-DE. The fluid engine is English only."),
    Option("source", "VOICE_NOTES_SOURCE", "both", "Audio sources",
           "both: microphone and system audio. mic: microphone only. system: system audio only.",
           choices=("both", "mic", "system")),
    Option("mic", "VOICE_NOTES_MIC", None, "Microphone",
           "Input device name or UID from `voice-notes devices`. Unset: the macOS default input."),
    Option("model", "VOICE_NOTES_MODEL", "claude-haiku-5-5", "Claude notes model",
           "Claude model alias or ID that writes the meeting notes."),
    Option("claude_cli", "VOICE_NOTES_CLAUDE_CLI", None, "Claude CLI",
           "Path to a Claude Code CLI to use instead of the one bundled with the Agent SDK. "
           "Unset: the bundled one.", kind="executable"),
    Option("speakers_dir", "VOICE_NOTES_SPEAKERS", str(Path.home() / ".voice-notes" / "speakers"),
           "Speaker registry", "Folder of enrolled voices for named speakers (fluid engine).", kind="path"),
    Option("port", "VOICE_NOTES_PORT", 8765, "Web interface port",
           "Port for `voice-notes serve`, the launcher and the login service.", kind="port"),
)
BY_KEY = {option.key: option for option in OPTIONS}


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or Path.home() / ".voice-notes-config.json").expanduser()


def validate(key: str, value):
    """Normalize one value for the config file; None means unset. Raises ValueError on bad input."""
    option = BY_KEY.get(key)
    if option is None:
        raise ValueError(f"Unknown setting {key!r}. Settings: {', '.join(BY_KEY)}")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if option.kind == "port":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number") from None
        if isinstance(value, bool) or not 1 <= number <= 65535:
            raise ValueError(f"{key} must be from 1 to 65535")
        return number
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    value = value.strip()
    if option.choices and value not in option.choices:
        raise ValueError(f"{key} must be one of: {', '.join(option.choices)}")
    if option.kind == "path":
        return str(Path(value).expanduser().resolve())
    if option.kind == "executable":
        found = str(Path(value).expanduser()) if os.sep in value else shutil.which(value)
        if not found or not os.path.isfile(found) or not os.access(found, os.X_OK):
            raise ValueError(f"{key}: not found or not executable: {value}")
        return str(Path(found).resolve())
    return value


def check(option: Option, value, origin: str):
    """A saved or environment value, checked without touching the filesystem (paths stay as written, and the
    Claude CLI is checked when it is used), so one stale entry never stops `configure` from fixing it."""
    if option.kind in ("path", "executable") and isinstance(value, str):
        return value.strip() or None
    try:
        return validate(option.key, value)
    except ValueError as error:
        print(f"voice-notes: ignoring {origin}: {error}", file=sys.stderr)
        return None


def load(path: Path | None = None) -> dict:
    """The saved settings. Unknown keys are ignored (and kept on save); invalid values are reported and skipped."""
    path = path or config_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        print(f"voice-notes: ignoring {path}: {error}. Fix it or run `voice-notes configure`.", file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        print(f"voice-notes: ignoring {path}: it must hold a JSON object.", file=sys.stderr)
        return {}
    settings = {}
    for key, value in raw.items():
        if key in BY_KEY and value is not None:
            if (checked := check(BY_KEY[key], value, f"{key} in {path}")) is not None:
                settings[key] = checked
    return settings


def save(changes: dict, path: Path | None = None) -> Path:
    """Merge validated changes into the file (None removes a key), keeping keys this version doesn't know."""
    path = path or config_path()
    try:
        current = json.loads(path.read_text())
        if not isinstance(current, dict):
            current = {}
    except (FileNotFoundError, ValueError):
        current = {}
    for key, value in changes.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    ordered = {option.key: current[option.key] for option in OPTIONS if option.key in current}
    ordered.update({key: value for key, value in current.items() if key not in ordered})
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(ordered, indent=2) + "\n")
    return path


def from_env(option: Option):
    value = os.environ.get(option.env, "").strip()
    return check(option, value, option.env) if value else None


def resolve(flags: dict | None = None, path: Path | None = None) -> tuple[dict, dict]:
    """Effective settings and where each came from: flag, env, config, or default."""
    flags = flags or {}
    saved = load(path)
    values, sources = {}, {}
    for option in OPTIONS:
        if flags.get(option.key) is not None:
            values[option.key], sources[option.key] = flags[option.key], "flag"
        elif (value := from_env(option)) is not None:
            values[option.key], sources[option.key] = value, "env"
        elif saved.get(option.key) is not None:
            values[option.key], sources[option.key] = saved[option.key], "config"
        else:
            values[option.key], sources[option.key] = option.default, "default"
    return values, sources


def describe(value) -> str:
    return "(unset)" if value is None else str(value)


def show(flags: dict | None = None) -> str:
    """Table of effective settings and where each comes from."""
    values, sources = resolve(flags)
    path = config_path()
    lines = [f"Config file: {path}" + ("" if path.exists() else " (not created yet; run `voice-notes configure`)"), ""]
    width = max(len(option.key) for option in OPTIONS)
    for option in OPTIONS:
        source = {"env": f"from {option.env}", "flag": "from the command line",
                  "config": "from the config file", "default": "default"}[sources[option.key]]
        lines.append(f"{option.key:<{width}}  {describe(values[option.key])}  ({source})")
    return "\n".join(lines)


def parse_assignment(text: str) -> tuple[str, object]:
    key, separator, value = text.partition("=")
    if not separator:
        raise ValueError(f"Expected KEY=VALUE, got {text!r}")
    key = key.strip().replace("-", "_")
    return key, validate(key, value)


def interactive(ask=input, say=print) -> dict:
    """Ask for each setting, showing the current value; returns the changes to save (None = unset)."""
    saved = load()
    say(f"Voice Notes settings, saved to {config_path()}")
    say("Press Enter to keep the value in brackets, or type - to go back to the default.\n")
    changes = {}
    for option in OPTIONS:
        current = saved.get(option.key)
        # The meetings folder is pinned to an absolute path so every launch, from any folder, finds it.
        if current is None and option.key == "meetings_dir":
            current = str(Path(option.default).resolve())
        shown = describe(current) if current is not None else f"default: {describe(option.default)}"
        choices = f" ({'/'.join(option.choices)})" if option.choices else ""
        say(f"{option.label}{choices}: {option.help}")
        if os.environ.get(option.env):
            say(f"  Note: {option.env} is set in your environment and overrides this.")
        while True:
            answer = ask(f"  [{shown}] ").strip()
            if not answer:
                value = current
            elif answer == "-":
                value = None
            else:
                try:
                    value = validate(option.key, answer)
                except ValueError as error:
                    say(f"  {error}")
                    continue
            break
        if value != saved.get(option.key):
            changes[option.key] = value
        say("")
    engine = changes.get("engine", saved.get("engine"))
    locale = changes.get("locale", saved.get("locale")) or BY_KEY["locale"].default
    if engine == "fluid" and not str(locale).lower().startswith("en"):
        say(f"Warning: the fluid engine transcribes English only; locale {locale} applies to the apple engine.")
    return changes
