"""Persistent speaker registry shared by recordings.

The Swift helper creates and updates one directory per speaker UUID:

    <root>/<uuid>/speaker.json   name, embedding, timestamps, optional merged_into
    <root>/<uuid>/sample.wav     up to 60 s of that speaker's speech

Transcript journals store only UUIDs. Names are looked up when a transcript is
rendered, so naming a speaker later corrects every past transcript.
"""
import json
import os
import re
import uuid as uuid_module
from datetime import datetime
from pathlib import Path

from .session import atomic_write, meeting_title, read_segments, render_transcript

DEFAULT_DIR = Path(os.environ.get("VOICE_NOTES_SPEAKERS") or Path.home() / ".voice-notes" / "speakers")
MAX_NAME_LENGTH = 100


def is_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid_module.UUID(value)).lower() == value.lower()
    except ValueError:
        return False


def short_id(identifier: str) -> str:
    return identifier[:8].lower()


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        raise ValueError("A speaker name cannot be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"A speaker name can have at most {MAX_NAME_LENGTH} characters")
    return name


class SpeakerRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_DIR
        # speaker.json contents keyed by path, reused while the file's mtime and size are unchanged.
        self._cache: dict[Path, tuple[tuple[int, int], dict]] = {}

    def cached(self) -> "CachedNames":
        """A display-name lookup memoized for one render; it does not see later registry changes."""
        return CachedNames(self)

    def _directory(self, identifier: str) -> Path:
        if not is_uuid(identifier):
            raise ValueError(f"Not a speaker UUID: {identifier!r}")
        return self.root / identifier.lower()

    def sample_path(self, identifier: str) -> Path:
        return self._directory(identifier) / "sample.wav"

    def read(self, identifier: str) -> dict | None:
        path = self._directory(identifier) / "speaker.json"
        try:
            info = path.stat()
            key = (info.st_mtime_ns, info.st_size)
            hit = self._cache.get(path)
            if hit and hit[0] == key:
                return dict(hit[1])
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            self._cache.pop(path, None)
            return None
        if not isinstance(data, dict):
            return None
        self._cache[path] = (key, data)
        return dict(data)

    def _write(self, identifier: str, data: dict) -> None:
        data["updated_at"] = datetime.now().astimezone().isoformat()
        atomic_write(self._directory(identifier) / "speaker.json", json.dumps(data, indent=2) + "\n")

    def resolve(self, identifier: str) -> str:
        """Follow merge tombstones to the surviving speaker."""
        seen = set()
        current = identifier.lower()
        while current not in seen:
            seen.add(current)
            data = self.read(current)
            target = data.get("merged_into") if data else None
            if not is_uuid(target):
                return current
            current = target.lower()
        return identifier.lower()

    def display_name(self, identifier: str) -> str:
        identifier = self.resolve(identifier)
        data = self.read(identifier)
        name = data.get("name") if data else None
        return name if isinstance(name, str) and name.strip() else short_id(identifier)

    def list(self) -> list[dict]:
        speakers = []
        if not self.root.is_dir():
            return speakers
        for entry in sorted(self.root.iterdir()):
            if not is_uuid(entry.name):
                continue
            data = self.read(entry.name)
            if not data or is_uuid(data.get("merged_into")):
                continue
            sample = entry / "sample.wav"
            speakers.append({
                "id": entry.name.lower(), "name": data.get("name"),
                "created_at": data.get("created_at"), "updated_at": data.get("updated_at"),
                "sample_seconds": float(data.get("sample_seconds") or 0),
                "has_sample": sample.is_file(),
            })
        return sorted(speakers, key=lambda speaker: speaker.get("created_at") or "")

    def find(self, reference: str) -> str:
        """Resolve a full UUID, a unique UUID prefix, or a unique name."""
        reference = reference.strip().lower()
        candidates = self.list()
        exact = [s["id"] for s in candidates if s["id"] == reference]
        if exact:
            return exact[0]
        matches = [s["id"] for s in candidates if len(reference) >= 4 and s["id"].startswith(reference)]
        if not matches:
            matches = [s["id"] for s in candidates if (s["name"] or "").lower() == reference]
        if not matches:
            raise ValueError(f"No speaker matches {reference!r}. Run: voice-notes speakers list")
        if len(matches) > 1:
            raise ValueError(f"{reference!r} matches {len(matches)} speakers; use more of the UUID")
        return matches[0]

    def set_name(self, identifier: str, name: str) -> str:
        identifier = self.resolve(identifier)
        data = self.read(identifier)
        if data is None:
            raise ValueError(f"Unknown speaker {identifier}")
        data["name"] = clean_name(name)
        self._write(identifier, data)
        return identifier

    def merge(self, source: str, target: str) -> str:
        source, target = self.resolve(source), self.resolve(target)
        if source == target:
            raise ValueError("Choose two different speakers to merge")
        source_data, target_data = self.read(source), self.read(target)
        if source_data is None or target_data is None:
            raise ValueError("Both speakers must exist")
        weight_a = float(source_data.get("embedding_seconds") or 0)
        weight_b = float(target_data.get("embedding_seconds") or 0)
        vector_a, vector_b = source_data.get("embedding"), target_data.get("embedding")
        if (isinstance(vector_a, list) and isinstance(vector_b, list)
                and len(vector_a) == len(vector_b) and weight_a + weight_b > 0):
            combined = [(a * weight_a + b * weight_b) / (weight_a + weight_b) for a, b in zip(vector_a, vector_b)]
            norm = sum(value * value for value in combined) ** 0.5 or 1.0
            target_data["embedding"] = [value / norm for value in combined]
            target_data["embedding_seconds"] = min(weight_a + weight_b, 120.0)
        if not (target_data.get("name") or "").strip() and source_data.get("name"):
            target_data["name"] = source_data["name"]
        self._write(target, target_data)
        # Keep a tombstone so journals that mention the old UUID still resolve.
        source_data.update(merged_into=target, embedding=None)
        self._write(source, source_data)
        (self._directory(source) / "sample.wav").unlink(missing_ok=True)
        return target

    def forget(self, identifier: str) -> None:
        """Delete a speaker's voice data. Old transcripts show it as unknown."""
        directory = self._directory(self.resolve(identifier))
        if not directory.is_dir():
            raise ValueError(f"Unknown speaker {identifier}")
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()


class CachedNames:
    """Memoized `display_name` for rendering many lines; speakers are read once each."""

    def __init__(self, registry: SpeakerRegistry):
        self.registry, self.names, self.resolved = registry, {}, {}

    def cached(self) -> "CachedNames":
        return self

    def resolve(self, identifier: str) -> str:
        if identifier not in self.resolved:
            self.resolved[identifier] = self.registry.resolve(identifier)
        return self.resolved[identifier]

    def display_name(self, identifier: str) -> str:
        if identifier not in self.names:
            self.names[identifier] = self.registry.display_name(identifier)
        return self.names[identifier]


def rerender_transcripts(meetings: Path, registry: SpeakerRegistry, speakers: set[str] | None = None) -> list[Path]:
    """Re-render transcript.md for meetings that mention the given speakers (or all of them)."""
    updated = []
    if not Path(meetings).is_dir():
        return updated
    affected = {registry.resolve(identifier) for identifier in speakers} if speakers else None
    names = registry.cached()
    for journal in sorted(Path(meetings).glob("*/events.jsonl")):
        try:
            segments = read_segments(journal)
        except (OSError, ValueError):
            continue
        mentioned = {names.resolve(s["speaker_uuid"]) for s in segments if s.get("speaker_uuid")}
        if not mentioned or (affected is not None and not (mentioned & affected)):
            continue
        atomic_write(journal.parent / "transcript.md",
                     render_transcript(segments, meeting_title(journal.parent), names))
        updated.append(journal.parent / "transcript.md")
    return updated
