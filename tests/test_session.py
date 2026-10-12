import json

import pytest

from voice_notes.session import read_segments, render_transcript


def segment(source="system", id="1", start=2, text="Ship on Friday.", **extra):
    return dict(type="segment", source=source, id=id, start=start, end=start + 1,
                text=text, **extra)


def test_revisions_replace_labels_and_tracks_have_independent_ids(tmp_path):
    events = [segment(), segment(source="mic", start=1, text="Agreed."),
              segment(speakers=[{"speaker_id": 8}])]
    journal = tmp_path / "events.jsonl"
    journal.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    result = read_segments(journal)
    assert len(result) == 2
    assert result[0]["source"] == "mic"
    assert result[1]["speakers"] == [{"speaker_id": 8}]
    text = render_transcript(result, "Planning")
    assert "speaker 8" in text
    assert text.count("Ship on Friday.") == 1


def test_truncated_final_line_recovers_previous_commits(tmp_path):
    journal = tmp_path / "events.jsonl"
    journal.write_text(json.dumps(segment()) + '\n{"type": "seg')
    assert len(read_segments(journal)) == 1


def test_corruption_before_final_line_is_reported(tmp_path):
    journal = tmp_path / "events.jsonl"
    journal.write_text('broken\n' + json.dumps(segment()) + "\n")
    with pytest.raises(ValueError, match="Corrupt"):
        read_segments(journal)


@pytest.mark.parametrize("extra", [{"start": float("nan")}, {"end": 0}, {"source": "unknown"}])
def test_invalid_segment_rejected(tmp_path, extra):
    event = segment()
    event.update(extra)
    journal = tmp_path / "events.jsonl"
    journal.write_text(json.dumps(event))
    with pytest.raises(ValueError):
        read_segments(journal)


def test_failed_atomic_replace_keeps_existing_file(tmp_path, monkeypatch):
    from voice_notes import session
    path = tmp_path / "notes.md"
    path.write_text("Previous notes")
    def fail(*args):
        raise OSError("Disk write failed")
    monkeypatch.setattr(session.os, "replace", fail)
    with pytest.raises(OSError, match="Disk write"):
        session.atomic_write(path, "New notes")
    assert path.read_text() == "Previous notes"
    assert list(tmp_path.iterdir()) == [path]


def test_non_object_journal_event_is_rejected(tmp_path):
    journal = tmp_path / "events.jsonl"
    journal.write_text('[]\n')
    with pytest.raises(ValueError, match="JSON objects"):
        read_segments(journal)
