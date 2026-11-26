"""Durable event journal; later diarization revisions replace earlier labels."""
import json
import math
import os
import tempfile
import uuid
from pathlib import Path


def timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def validate_segment(event: dict) -> None:
    if event.get("source") not in ("mic", "system"):
        raise ValueError("Unknown transcript source")
    for key in ("start", "end"):
        if not isinstance(event.get(key), (int, float)) or not math.isfinite(event[key]):
            raise ValueError(f"Invalid segment {key}")
    if event["start"] < 0 or event["end"] < event["start"]:
        raise ValueError("Invalid segment time range")
    if not isinstance(event.get("text"), str) or not isinstance(event.get("id"), str):
        raise ValueError("Invalid segment text or id")
    speaker = event.get("speaker_uuid")
    if speaker is not None:
        try:
            valid = isinstance(speaker, str) and str(uuid.UUID(speaker)) == speaker.lower()
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Invalid speaker UUID")


def read_segments(path: Path) -> list[dict]:
    latest = {}
    with path.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A crash can leave one truncated final journal line.
                if stream.read().strip():
                    raise ValueError("Corrupt transcript journal before its final line")
                break
            if not isinstance(event, dict):
                raise ValueError("Transcript journal events must be JSON objects")
            if event.get("type") == "segment":
                validate_segment(event)
                latest[(event["source"], event["id"])] = event
    return sorted(latest.values(), key=lambda s: (s["start"], s["source"], s["id"]))


def speaker_label(segment: dict, registry=None) -> str:
    """Name a speaker from the registry; journals store UUIDs, never names."""
    identifier = segment.get("speaker_uuid")
    if identifier:
        if registry is not None:
            return registry.display_name(identifier)
        return f"Unknown speaker {identifier[:8].lower()}"
    return "Microphone" if segment["source"] == "mic" else "Remote audio"


def render_transcript(segments: list[dict], title: str, registry=None) -> str:
    rows = [f"# {title.replace(chr(10), ' ')}", "", "## Transcript", ""]
    for segment in segments:
        if not segment["text"].strip():
            continue
        label = speaker_label(segment, registry)
        text = segment["text"].strip().replace("\n", " ")
        rows.append(f"[{timestamp(segment['start'])}] **{label}:** {text}\n")
    return "\n".join(rows) + "\n"


def atomic_write(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

