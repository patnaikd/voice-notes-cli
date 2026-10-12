"""Durable event journal; later diarization revisions replace earlier labels."""
import json
import math
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
            if event.get("type") == "segment":
                validate_segment(event)
                latest[(event["source"], event["id"])] = event
    return sorted(latest.values(), key=lambda s: (s["start"], s["source"], s["id"]))


def render_transcript(segments: list[dict], title: str) -> str:
    rows = [f"# {title.replace(chr(10), ' ')}", "", "## Transcript", ""]
    for segment in segments:
        if not segment["text"].strip():
            continue
        label = "Microphone" if segment["source"] == "mic" else "Remote audio"
        speakers = sorted({str(s["speaker_id"]) for s in segment.get("speakers", [])
                           if s.get("speaker_id") is not None and s["speaker_id"] >= 0})
        if speakers and segment["source"] == "system":
            label += " (speaker " + ", ".join(speakers) + ")"
        text = segment["text"].strip().replace("\n", " ")
        rows.append(f"[{timestamp(segment['start'])}] **{label}:** {text}\n")
    return "\n".join(rows) + "\n"


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content)
    temporary.replace(path)

