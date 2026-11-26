"""launchd agent that keeps `voice-notes serve` running (RunAtLoad + KeepAlive)."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "local.voice-notes.web"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(*, port: int, data_dir: Path, working_dir: Path, build_dir: Path, speakers_dir: Path) -> dict:
    logs = Path.home() / "Library" / "Logs" / "voice-notes"
    source_root = Path(__file__).resolve().parents[2]
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
                   "PYTHONPATH": str(source_root), "PYTHONUNBUFFERED": "1"}
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "voice_notes.cli", "--build-dir", str(build_dir.resolve()),
                             "--speakers-dir", str(Path(speakers_dir).resolve()), "serve", "--port", str(port),
                             "--data-dir", str(data_dir.resolve())],
        "WorkingDirectory": str(working_dir.resolve()),
        "EnvironmentVariables": environment,
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
        definition = build_plist(port=args.port, data_dir=args.data_dir, working_dir=Path.cwd(),
                                 build_dir=args.build_dir, speakers_dir=args.speakers_dir)
        Path(definition["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        launchctl("bootout", f"{domain}/{LABEL}")
        path.write_bytes(plistlib.dumps(definition))
        result = launchctl("bootstrap", domain, str(path))
        if result.returncode:
            raise RuntimeError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
        print(f"Installed {path}\nVoice Notes now runs at login: http://localhost:{args.port}/\n"
              f"Logs: {Path(definition['StandardErrorPath']).parent}\n"
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
