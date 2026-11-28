"""index.json / state.json / jobs.json persistence and the segment/meeting model."""
import asyncio
import functools
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from ..session import atomic_write, cached_names, read_segments, render_transcript, speaker_label

log = logging.getLogger("voice_notes.web")
SEGMENT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?$")
DEFAULT_SETTINGS = {"engine": "fluid", "source": "both", "mic": None, "locale": "en-US", "model": "claude-haiku-5-5",
                    "segment_minutes": 30, "auto_merge": True, "auto_merge_min_confidence": 0.6}
OPEN = ("recording", "closing")


def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(timespec="milliseconds")


@functools.lru_cache(maxsize=65536)
def epoch(value: str) -> float:
    # Cached: sorting and neighbor lookups convert every segment's start time repeatedly.
    return datetime.fromisoformat(value).timestamp()


def order(segment: dict) -> tuple[float, str]:
    """Sort key; ISO strings with different UTC offsets do not sort chronologically."""
    return epoch(segment["started_at"]), segment["id"]


def clock(value: float, seconds=False) -> str:
    return datetime.fromtimestamp(value).strftime("%H:%M:%S" if seconds else "%H:%M")


def nest_markdown(text: str, top: int = 3) -> str:
    """Drop a leading H1 (the meeting title, shown separately) and shift headings so the highest becomes `top`."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    fenced, headings = False, []
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,6} ", line):
            headings.append(index)
    if headings:
        shift = top - min(len(lines[i]) - len(lines[i].lstrip("#")) for i in headings)
        for i in headings:
            hashes = len(lines[i]) - len(lines[i].lstrip("#"))
            lines[i] = "#" * max(1, min(6, hashes + shift)) + lines[i][hashes:]
    return "\n".join(lines).strip()


NOTES_MISSING = {"none": "No notes yet.", "queued": "Notes are queued.", "running": "Notes are being written.",
                 "skipped": "No speech, so no notes.", "failed": "Notes failed."}


def validate_settings(changes: dict, current: dict) -> dict:
    """Merge user changes into settings; raises ValueError on bad values."""
    result = dict(current)
    for key, value in changes.items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown setting {key!r}")
        if key == "engine" and value not in ("apple", "fluid"):
            raise ValueError("engine must be apple or fluid")
        if key == "source" and value not in ("both", "mic", "system"):
            raise ValueError("source must be both, mic, or system")
        if key == "mic" and value is not None and not isinstance(value, str):
            raise ValueError("mic must be a string or null")
        if key == "locale" and (not isinstance(value, str) or not value.strip()):
            raise ValueError("locale must be a non-empty string")
        if key == "model" and (not isinstance(value, str) or not value.strip()):
            raise ValueError("model must be a non-empty string")
        if key == "segment_minutes" and (not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 720):
            raise ValueError("segment_minutes must be an integer from 1 to 720")
        if key == "auto_merge" and not isinstance(value, bool):
            raise ValueError("auto_merge must be true or false")
        if key == "auto_merge_min_confidence" and (not isinstance(value, (int, float)) or not 0 <= value <= 1):
            raise ValueError("auto_merge_min_confidence must be between 0 and 1")
        result[key] = value or None if key == "mic" else value
    return result


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        log.error("Cannot read %s (%s); moved aside to %s", path, error, backup.name)
        path.replace(backup)
        return default


class Store:
    FLUSH_DELAY = 1.0  # seconds

    def __init__(self, root: Path):
        self._dirty: set[str] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._write_lock = threading.Lock()
        self._sequence = 0
        self._written: dict[str, int] = {}
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        index = read_json(self.root / "index.json", {})
        self.segments: dict[str, dict] = index.get("segments") or {}
        self.meetings: dict[str, dict] = index.get("meetings") or {}
        state = read_json(self.root / "state.json", {})
        self.recording_desired = bool(state.get("recording_desired"))
        try:
            self.settings = validate_settings(state.get("settings") or {}, DEFAULT_SETTINGS)
        except ValueError as error:
            log.error("Ignoring invalid saved settings: %s", error)
            self.settings = dict(DEFAULT_SETTINGS)
        self.jobs: list[dict] = read_json(self.root / "jobs.json", {}).get("jobs") or []

    # index.json and jobs.json change many times a minute and grow with history. Inside the event loop,
    # saves are coalesced into at most one write per FLUSH_DELAY, done on a writer thread; the journals stay
    # the source of truth, and startup recovery rebuilds what a crash loses. Outside a loop they write now.

    def save_index(self):
        self._save("index")

    def save_jobs(self):
        self._save("jobs")

    def _save(self, name: str):
        self._dirty.add(name)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._flush_handle is None:
            self._flush_handle = loop.call_later(self.FLUSH_DELAY, self._flush_later, loop)

    def _flush_later(self, loop):
        self._flush_handle = None
        for name, content, sequence in self._serialize():
            loop.run_in_executor(None, self._write, name, content, sequence)

    def flush(self):
        """Write pending index/jobs changes now (shutdown, or code running outside the event loop)."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        for name, content, sequence in self._serialize():
            self._write(name, content, sequence)

    def _serialize(self) -> list[tuple[str, str, int]]:
        """Snapshot dirty files as text on the calling thread, which owns the in-memory model."""
        output = []
        for name in sorted(self._dirty):
            if name == "index":
                content = json.dumps({"version": 1, "segments": self.segments, "meetings": self.meetings})
            else:
                active = [job for job in self.jobs if job["status"] in ("queued", "running")]
                finished = [job for job in self.jobs if job["status"] not in ("queued", "running")][-200:]
                self.jobs[:] = sorted(active + finished, key=lambda job: job["created_at"])
                content = json.dumps({"jobs": self.jobs})
            self._sequence += 1
            output.append((name, content + "\n", self._sequence))
        self._dirty.clear()
        return output

    def _write(self, name: str, content: str, sequence: int):
        with self._write_lock:  # never let an older snapshot replace a newer one
            if sequence < self._written.get(name, 0):
                return
            atomic_write(self.root / f"{name}.json", content)
            self._written[name] = sequence

    def save_state(self):
        atomic_write(self.root / "state.json", json.dumps(
            {"recording_desired": self.recording_desired, "settings": self.settings}, indent=2) + "\n")

    # Paths

    def segment_dir(self, segment_id: str) -> Path:
        if not SEGMENT_NAME.match(segment_id):
            raise ValueError(f"Invalid segment id {segment_id!r}")
        return self.root / segment_id

    def notes_dir(self, meeting: dict) -> Path:
        if len(meeting["segment_ids"]) == 1:
            return self.segment_dir(meeting["segment_ids"][0])
        return self.root / "merged" / meeting["id"]

    def new_segment_id(self, start: float) -> str:
        base = datetime.fromtimestamp(start).strftime("%Y-%m-%d_%H-%M-%S")
        name, number = base, 1
        while name in self.segments or (self.root / name).exists():
            number += 1
            name = f"{base}-{number}"
        return name

    # Model

    def ordered_segments(self) -> list[dict]:
        return sorted(self.segments.values(), key=order)

    def previous_segment(self, segment: dict) -> dict | None:
        key = order(segment)
        return max((s for s in self.segments.values() if order(s) < key), key=order, default=None)

    def new_meeting(self, segment: dict, title: str | None = None) -> dict:
        meeting_id = secrets.token_hex(6)
        while meeting_id in self.meetings:
            meeting_id = secrets.token_hex(6)
        meeting = {"id": meeting_id, "title": title or "Meeting " + clock(epoch(segment["started_at"])),
                   "segment_ids": [segment["id"]], "started_at": segment["started_at"], "ended_at": segment["ended_at"],
                   "notes_status": "none", "notes_error": None, "notes_stale": False,
                   "merged_by": None, "auto_merge_reason": None}
        self.meetings[meeting_id] = meeting
        segment["meeting_id"] = meeting_id
        return meeting

    def refresh_meeting(self, meeting: dict) -> dict:
        segments = sorted((self.segments[i] for i in meeting["segment_ids"] if i in self.segments), key=order)
        meeting["segment_ids"] = [s["id"] for s in segments]
        if segments:
            meeting["started_at"] = segments[0]["started_at"]
            ended = [s["ended_at"] for s in segments]
            meeting["ended_at"] = None if None in ended or any(s["status"] in OPEN for s in segments) else max(ended)
        return meeting

    def merge(self, meeting_ids: list[str], merged_by: str, reason: str | None = None) -> tuple[dict, list[str]]:
        """Combine meetings into the earliest one; returns it and the removed ids."""
        meetings = sorted((self.meetings[i] for i in dict.fromkeys(meeting_ids)), key=lambda m: epoch(m["started_at"]))
        if len(meetings) < 2:
            raise ValueError("Choose at least two different meetings to merge")
        keep, removed = meetings[0], meetings[1:]
        for meeting in removed:
            keep["segment_ids"] += meeting["segment_ids"]
            for segment_id in meeting["segment_ids"]:
                self.segments[segment_id]["meeting_id"] = keep["id"]
            del self.meetings[meeting["id"]]
        keep.update(merged_by=merged_by, auto_merge_reason=reason)
        self.refresh_meeting(keep)
        return keep, [m["id"] for m in removed]

    def split(self, meeting_id: str) -> list[dict]:
        meeting = self.meetings[meeting_id]
        if len(meeting["segment_ids"]) < 2:
            raise ValueError("Only a merged meeting can be split")
        first, *rest = meeting["segment_ids"]
        meeting.update(segment_ids=[first], merged_by=None, auto_merge_reason=None)
        result = [self.refresh_meeting(meeting)]
        for segment_id in rest:
            result.append(self.refresh_meeting(self.new_meeting(self.segments[segment_id])))
        return result

    def trash_folders(self, meeting_id: str, segment_ids: list[str]) -> Path:
        """Move a meeting's folders to trash/ (recoverable by moving them back). Files only, so it can run
        on a worker thread; `forget_meeting` then drops the meeting from the index on the event loop."""
        trash = self.root / "trash" / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{meeting_id}"
        trash.mkdir(parents=True)
        for segment_id in segment_ids:
            folder = self.segment_dir(segment_id)
            if folder.exists():
                os.replace(folder, trash / segment_id)
        merged = self.root / "merged" / meeting_id
        if merged.exists():
            os.replace(merged, trash / "merged")
        return trash

    def forget_meeting(self, meeting_id: str, segment_ids: list[str]) -> None:
        for segment_id in segment_ids:
            self.segments.pop(segment_id, None)
        self.meetings.pop(meeting_id, None)

    def export_day(self, day) -> str | None:
        """Markdown with the notes of every meeting that started on `day` (a date), or None if there are none."""
        meetings = sorted((m for m in self.meetings.values()
                           if datetime.fromtimestamp(epoch(m["started_at"])).date() == day),
                          key=lambda m: epoch(m["started_at"]))
        if not meetings:
            return None
        rows = [f"# Meeting notes: {day:%A, %B} {day.day}, {day:%Y}", "",
                f"{len(meetings)} {'meeting' if len(meetings) == 1 else 'meetings'}", ""]
        for meeting in meetings:
            start = epoch(meeting["started_at"])
            end = clock(epoch(meeting["ended_at"])) if meeting["ended_at"] else "now"
            segments = len(meeting["segment_ids"])
            rows += [f"## {meeting['title']}", "",
                     f"*{clock(start)}–{end} · {segments} {'segment' if segments == 1 else 'segments'}*", ""]
            notes = self.notes_dir(meeting) / "notes.md"
            if notes.exists():
                if meeting.get("notes_stale"):
                    rows += ["> The transcript was edited after these notes were written.", ""]
                rows += [nest_markdown(notes.read_text()), ""]
            else:
                reason = NOTES_MISSING.get(meeting["notes_status"], "No notes.")
                if meeting["notes_status"] == "failed" and meeting.get("notes_error"):
                    reason = f"Notes failed: {meeting['notes_error']}"
                rows += [f"*{reason}*", ""]
        return "\n".join(rows).rstrip() + "\n"

    # Transcripts

    def delete_lines(self, segment: dict, keys: list[tuple[str, str]], title: str, registry=None) -> None:
        """Append deletion events for (source, id) lines, then re-render the segment transcript."""
        journal = self.segment_dir(segment["id"]) / "events.jsonl"
        events = read_segments(journal) if journal.exists() else []
        present = {(event["source"], event["id"]) for event in events}
        missing = [line_id for source, line_id in keys if (source, line_id) not in present]
        if missing:
            raise KeyError(f"Line(s) not found in segment {segment['id']}: {', '.join(missing)}")
        with journal.open("a") as stream:
            for source, line_id in keys:
                stream.write(json.dumps({"type": "delete", "source": source, "id": line_id}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Apply the deletions in memory instead of parsing the journal a second time.
        deleted = set(keys)
        self.finish_segment(segment, title, registry,
                            events=[event for event in events if (event["source"], event["id"]) not in deleted])

    def segment_lines(self, segment: dict, registry=None) -> list[dict]:
        journal = self.segment_dir(segment["id"]) / "events.jsonl"
        if not journal.exists():
            return []
        start, registry = epoch(segment["started_at"]), cached_names(registry)
        return [line_payload(segment["id"], event, start, registry) for event in read_segments(journal)
                if event["text"].strip()]

    def meeting_transcript(self, meeting: dict, registry=None) -> tuple[str, list[str]]:
        """Markdown with wall-clock times plus the raw line texts (for checks)."""
        rows, texts = [f"# {meeting['title'].replace(chr(10), ' ')}", ""], []
        registry = cached_names(registry)
        for segment_id in meeting["segment_ids"]:
            segment = self.segments[segment_id]
            start = epoch(segment["started_at"])
            end = epoch(segment["ended_at"]) if segment["ended_at"] else time.time()
            rows += [f"## {clock(start)}–{clock(end)}", ""]
            lines = self.segment_lines(segment, registry)
            for line in lines:
                rows.append(f"[{clock(start + line['start'], True)}] **{line['speaker']}:** "
                            f"{line['text'].strip().replace(chr(10), ' ')}\n")
                texts.append(line["text"])
            if not lines:
                rows += ["*No speech.*", ""]
        return "\n".join(rows) + "\n", texts

    def finish_segment(self, segment: dict, title: str, registry=None, events: list[dict] | None = None) -> None:
        """Render transcript.md from the journal (or already-parsed `events`) and set status, line count and metadata."""
        directory = self.segment_dir(segment["id"])
        journal = directory / "events.jsonl"
        journal.touch()
        if events is None:
            events = read_segments(journal)
        atomic_write(directory / "transcript.md", render_transcript(events, title, registry))
        segment["line_count"] = sum(1 for event in events if event["text"].strip())
        segment["status"] = "closed" if segment["line_count"] else "empty"
        if not segment["ended_at"]:
            # Recovery: the journal's last write is the best end estimate.
            segment["ended_at"] = iso(max(journal.stat().st_mtime, epoch(segment["started_at"])))
        metadata = directory / "meeting.json"
        info = read_json(metadata, {}) if metadata.exists() else {}
        info.update(segment_end=segment["ended_at"])
        atomic_write(metadata, json.dumps(info, indent=2) + "\n")

    def import_folders(self, registry=None) -> list[str]:
        """Index CLI recordings (and orphaned segment folders) as closed one-segment meetings."""
        imported = []
        for directory in sorted(self.root.iterdir()):
            if (directory.name in self.segments or not SEGMENT_NAME.match(directory.name)
                    or not (directory / "meeting.json").is_file()):
                continue
            try:
                info = json.loads((directory / "meeting.json").read_text())
                started = info.get("segment_start") or info.get("started_at") or iso(directory.stat().st_mtime)
                segment = {"id": directory.name, "started_at": iso(epoch(started)), "ended_at": info.get("segment_end"),
                           "status": "closed", "line_count": 0, "meeting_id": "", "engine": info.get("engine", "apple"),
                           "continuation": None, "error": None}
                self.finish_segment(segment, info.get("title") or "Meeting", registry)
            except (OSError, ValueError, TypeError, KeyError) as error:
                log.warning("Skipping %s: %s", directory.name, error)
                continue
            self.segments[segment["id"]] = segment
            title = info.get("title") if info.get("kind") != "segment" and info.get("title") != "Meeting" else None
            meeting = self.new_meeting(segment, title)
            meeting["notes_status"] = ("done" if (directory / "notes.md").exists()
                                       else "skipped" if segment["status"] == "empty" else "none")
            imported.append(segment["id"])
        return imported


def line_payload(segment_id: str, event: dict, segment_start: float, registry=None) -> dict:
    return {"segment_id": segment_id, "id": event["id"], "source": event["source"], "start": event["start"],
            "end": event["end"], "wall": iso(segment_start + event["start"]), "text": event["text"],
            "speaker": speaker_label(event, registry), "speaker_uuid": event.get("speaker_uuid")}
