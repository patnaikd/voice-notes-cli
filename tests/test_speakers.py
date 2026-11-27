import json

import pytest

from voice_notes.session import read_segments, render_transcript
from voice_notes.speakers import SpeakerRegistry, clean_name, rerender_transcripts

ALICE = "0f8fad5b-d9cb-469f-a165-70867728950e"
BOB = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CAROL = "7c9e6679-aaaa-40de-944b-e07fc1f90ae7"


def add_speaker(root, identifier, name=None, embedding=None, seconds=10.0, sample=True):
    directory = root / identifier
    directory.mkdir(parents=True)
    (directory / "speaker.json").write_text(json.dumps({
        "id": identifier, "name": name, "created_at": "2026-01-01T00:00:00+00:00",
        "embedding": embedding or [1.0, 0.0], "embedding_seconds": seconds, "sample_seconds": 12.5}))
    if sample:
        (directory / "sample.wav").write_bytes(b"RIFF")


def segment(speaker=None, start=0, text="Hello.", source="mic", id="1"):
    event = dict(type="segment", source=source, id=id, start=start, end=start + 1, text=text)
    if speaker:
        event["speaker_uuid"] = speaker
    return event


def meeting(root, name, events, title="Weekly planning"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (directory / "meeting.json").write_text(json.dumps({"title": title}))
    return directory


def test_unnamed_speakers_are_unknown_until_named(tmp_path):
    add_speaker(tmp_path, ALICE)
    registry = SpeakerRegistry(tmp_path)
    assert registry.display_name(ALICE) == "0f8fad5b"
    registry.set_name(ALICE, "  Alice \n Smith ")
    assert registry.display_name(ALICE) == "Alice Smith"
    assert registry.display_name(BOB) == "7c9e6679"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_invalid_names_are_rejected(name):
    with pytest.raises(ValueError):
        clean_name(name)


def test_find_accepts_unique_prefix_or_name_and_rejects_ambiguity(tmp_path):
    add_speaker(tmp_path, ALICE, name="Alice")
    add_speaker(tmp_path, BOB)
    add_speaker(tmp_path, CAROL)
    registry = SpeakerRegistry(tmp_path)
    assert registry.find("0f8f") == ALICE
    assert registry.find("alice") == ALICE
    assert registry.find(BOB) == BOB
    with pytest.raises(ValueError, match="matches 2"):
        registry.find("7c9e")
    with pytest.raises(ValueError, match="No speaker"):
        registry.find("zzzz")


def test_merge_leaves_a_tombstone_that_resolves_old_uuids(tmp_path):
    add_speaker(tmp_path, ALICE, name="Alice", embedding=[1.0, 0.0])
    add_speaker(tmp_path, BOB, embedding=[0.0, 1.0])
    registry = SpeakerRegistry(tmp_path)
    assert registry.merge(ALICE, BOB) == BOB
    assert registry.resolve(ALICE) == BOB
    assert registry.display_name(ALICE) == "Alice"  # the name moved to the survivor
    assert [s["id"] for s in registry.list()] == [BOB]
    assert not (tmp_path / ALICE / "sample.wav").exists()
    merged = registry.read(BOB)
    assert merged["embedding"] == pytest.approx([2 ** -0.5, 2 ** -0.5])
    with pytest.raises(ValueError):
        registry.merge(BOB, ALICE)  # resolves to the same speaker


def test_forget_deletes_voice_data_but_old_transcripts_still_render(tmp_path):
    add_speaker(tmp_path / "speakers", ALICE, name="Alice")
    registry = SpeakerRegistry(tmp_path / "speakers")
    registry.forget(ALICE)
    assert not (tmp_path / "speakers" / ALICE).exists()
    assert "**0f8fad5b:**" in render_transcript([segment(ALICE)], "Title", registry)


def test_renaming_rerenders_only_meetings_that_mention_the_speaker(tmp_path):
    add_speaker(tmp_path / "speakers", ALICE)
    add_speaker(tmp_path / "speakers", BOB)
    registry = SpeakerRegistry(tmp_path / "speakers")
    meetings = tmp_path / "meetings"
    first = meeting(meetings, "a", [segment(ALICE, text="Ship Friday.")])
    second = meeting(meetings, "b", [segment(BOB, text="Agreed.")])
    registry.set_name(ALICE, "Alice")
    changed = rerender_transcripts(meetings, registry, {ALICE})
    assert changed == [first / "transcript.md"]
    assert "**Alice:** Ship Friday." in (first / "transcript.md").read_text()
    assert not (second / "transcript.md").exists()
    # Journals keep UUIDs only, so a later rename corrects the transcript again.
    assert ALICE in (first / "events.jsonl").read_text()
    assert "Alice" not in (first / "events.jsonl").read_text()
    registry.set_name(ALICE, "Alice Smith")
    rerender_transcripts(meetings, registry, {ALICE})
    assert "**Alice Smith:**" in (first / "transcript.md").read_text()


def test_merged_speakers_correct_past_meetings(tmp_path):
    add_speaker(tmp_path / "speakers", ALICE)
    add_speaker(tmp_path / "speakers", BOB, name="Bob")
    registry = SpeakerRegistry(tmp_path / "speakers")
    meetings = tmp_path / "meetings"
    old = meeting(meetings, "old", [segment(ALICE, text="Earlier voice.")])
    registry.merge(ALICE, BOB)
    assert rerender_transcripts(meetings, registry, {BOB}) == [old / "transcript.md"]
    assert "**Bob:** Earlier voice." in (old / "transcript.md").read_text()


def test_transcript_without_speaker_ids_keeps_source_labels():
    text = render_transcript([segment(source="mic"), segment(source="system", start=2, id="2")], "T")
    assert "**Microphone:**" in text and "**Remote audio:**" in text


def test_invalid_speaker_uuid_in_journal_is_rejected(tmp_path):
    journal = tmp_path / "events.jsonl"
    journal.write_text(json.dumps(segment("not-a-uuid")) + "\n")
    with pytest.raises(ValueError, match="speaker UUID"):
        read_segments(journal)


def test_registry_reads_are_cached_until_the_file_changes(tmp_path, monkeypatch):
    add_speaker(tmp_path, ALICE, name="Alice")
    registry = SpeakerRegistry(tmp_path)
    assert registry.display_name(ALICE) == "Alice"
    reads = []
    original = type(tmp_path).read_text
    monkeypatch.setattr(type(tmp_path), "read_text", lambda self, *a, **kw: reads.append(self) or original(self, *a, **kw))
    names = registry.cached()
    assert [names.display_name(ALICE) for _ in range(50)] == ["Alice"] * 50
    assert reads == []  # unchanged speaker.json is not re-read
    registry.set_name(ALICE, "Alicia")  # rewrites the file, so the cache is invalidated
    assert registry.display_name(ALICE) == "Alicia"
