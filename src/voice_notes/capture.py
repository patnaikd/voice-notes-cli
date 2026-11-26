"""Helper command line, event parsing, and device listing shared by the CLI and the web server."""
import json
import os
import subprocess
import sys
from pathlib import Path

from .session import validate_segment


def startup_timeout(engine: str) -> float:
    # The fluid engine loads about 1 GB of models, and downloads them on first use.
    return 600 if engine == "fluid" else 120


# Fluid engine models committed to the repository with Git LFS (see models/README.md).
REPO_MODELS = Path(__file__).resolve().parents[2] / "models" / "fluid"
# A file Git LFS has not fetched yet is a small text pointer starting with this.
LFS_POINTER = b"version https://git-lfs"


def _is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(len(LFS_POINTER)) == LFS_POINTER
    except OSError:
        return False


def fluid_models_dir() -> Path | None:
    """Local Fluid model folder: VOICE_NOTES_MODELS, else the repository's models/fluid.

    Returns None (FluidAudio then uses and fills its own cache) when the folder is
    missing or still holds unfetched Git LFS pointers, which CoreML cannot load.
    """
    configured = os.environ.get("VOICE_NOTES_MODELS")
    directory = Path(configured).expanduser().resolve() if configured else REPO_MODELS
    if not directory.is_dir():
        return None
    if any(_is_lfs_pointer(path) for path in directory.rglob("weight*.bin")):
        print(f"voice-notes: {directory} holds Git LFS pointers, not models; run `git lfs pull`. "
              "Using FluidAudio's download cache instead.", file=sys.stderr)
        return None
    return directory


def helper_arguments(*, engine: str, source: str, app: str = "all", locale: str = "en-US", mic: str | None = None,
                     speakers_dir: Path | None = None, speaker_threshold: float = 0.70,
                     input_file: Path | None = None, input_realtime: bool = False,
                     models_dir: Path | None = None) -> list[str]:
    arguments = ["--engine", engine, "--source", source, "--app", app, "--locale", locale, "--mic", mic or ""]
    if engine == "fluid":
        arguments += ["--speakers-dir", str(Path(speakers_dir).resolve()), "--speaker-threshold", str(speaker_threshold)]
        if models_dir:
            arguments += ["--models-dir", str(Path(models_dir).resolve())]
        if input_file:
            arguments += ["--input-file", str(Path(input_file).resolve())]
            if input_realtime:
                arguments.append("--input-realtime")
    return arguments


def parse_event(line: str) -> dict:
    """Decode one helper stdout line; raises ValueError for malformed events."""
    event = json.loads(line)
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("expected a JSON event object")
    if event["type"] == "segment":
        validate_segment(event)
    return event


def list_devices(command: list[str], timeout: float = 15) -> dict:
    """Run `<helper> --devices` and split devices into microphones and outputs."""
    result = subprocess.run([*command, "--devices"], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    devices = json.loads(result.stdout)["devices"]
    return {
        "microphones": [{"name": d["name"], "uid": d["uid"], "default": bool(d["default_input"])}
                        for d in devices if d["input_channels"] > 0],
        "outputs": [{"name": d["name"], "uid": d["uid"], "default": bool(d["default_output"])}
                    for d in devices if d["output_channels"] > 0],
    }
