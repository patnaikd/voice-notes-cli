"""launchd agent that keeps `voice-notes serve` running (RunAtLoad + KeepAlive)."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "local.voice-notes.web"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def pinned_environment(values: dict, sources: dict) -> dict:
    """Variables that give the agent this shell's choices: the config file in use, and settings that came
    from a flag or the environment. Settings from the config file are not copied, so the agent reads
    ~/.voice-notes-config.json itself each time it starts and later edits apply after a restart."""
    from .. import config
    environment = {}
    if os.environ.get(config.CONFIG_ENV):
        environment[config.CONFIG_ENV] = str(config.config_path().resolve())
    for option in config.OPTIONS:
        if option.key in ("meetings_dir", "port"):
            continue  # passed as serve flags
        if sources.get(option.key) in ("flag", "env") and values.get(option.key) is not None:
            value = values[option.key]
            environment[option.env] = str(Path(value).resolve()) if option.kind == "path" else str(value)
    return environment


def build_plist(*, working_dir: Path, build_dir: Path, port: int | None = None, data_dir: Path | None = None,
                environment: dict | None = None) -> dict:
    """port and data_dir are pinned only when given (from a flag or the environment); otherwise the
    agent's `serve` reads them from the config file."""
    logs = Path.home() / "Library" / "Logs" / "voice-notes"
    source_root = Path(__file__).resolve().parents[2]
    serve = ["serve"]
    if port is not None:
        serve += ["--port", str(port)]
    if data_dir is not None:
        serve += ["--data-dir", str(Path(data_dir).resolve())]
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "voice_notes.cli", "--build-dir", str(build_dir.resolve()),
                             *serve],
        "WorkingDirectory": str(working_dir.resolve()),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
                                 "PYTHONPATH": str(source_root), "PYTHONUNBUFFERED": "1", **(environment or {})},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / "server.out.log"),
        "StandardErrorPath": str(logs / "server.err.log"),
    }


def launchctl(*arguments, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, check=check)


def main(args) -> int:
    domain, path = f"gui/{os.getuid()}", plist_path()
    if args.action == "install":
        sources, pinned = args.config_sources, ("flag", "env")
        definition = build_plist(working_dir=Path.cwd(), build_dir=args.build_dir,
                                 port=args.port if sources["port"] in pinned else None,
                                 data_dir=args.data_dir if sources["meetings_dir"] in pinned else None,
                                 environment=pinned_environment(args.settings, sources))
        Path(definition["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        launchctl("bootout", f"{domain}/{LABEL}")
        path.write_bytes(plistlib.dumps(definition))
        result = launchctl("bootstrap", domain, str(path))
        if result.returncode:
            raise RuntimeError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
        print(f"Installed {path}\nVoice Notes now runs at login: http://localhost:{args.port}/\n"
              f"Logs: {Path(definition['StandardErrorPath']).parent}\n"
              "It reads ~/.voice-notes-config.json each time it starts; after `voice-notes configure`, "
              "run `voice-notes service install` again to restart it.\n"
              "The first recording asks macOS for Microphone and Screen & System Audio Recording permission "
              "for the capture helper; grant both, then restart recording.")
    elif args.action == "uninstall":
        launchctl("bootout", f"{domain}/{LABEL}")
        path.unlink(missing_ok=True)
        print(f"Removed {LABEL}. Recordings and settings are kept.")
    else:
        result = launchctl("print", f"{domain}/{LABEL}")
        if result.returncode:
            print(f"{LABEL} is not loaded" + (f" (plist exists: {path})" if path.exists() else ""))
            return 1
        keys = ("state", "pid", "last exit code", "runs", "program", "working directory")
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if any(stripped.startswith(key + " =") for key in keys):
                print(stripped)
    return 0
