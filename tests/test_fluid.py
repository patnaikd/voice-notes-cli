import json
import os
import platform
import shutil
import subprocess

import pytest

from voice_notes import cli
from voice_notes.speakers import SpeakerRegistry

ALICE = "0f8fad5b-d9cb-469f-a165-70867728950e"
BOB = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def enroll(root, identifier, name=None):
    (root / identifier).mkdir(parents=True)
    (root / identifier / "speaker.json").write_text(json.dumps(
        {"id": identifier, "name": name, "created_at": "2026-01-01T00:00:00+00:00",
         "embedding": [1.0, 0.0], "embedding_seconds": 5, "sample_seconds": 3}))
    (root / identifier / "sample.wav").write_bytes(b"RIFF")


def fluid_helper(tmp_path, events):
    """A stand-in helper that records its arguments and plays back fluid events."""
    helper = tmp_path / "fake-fluid"
    arguments = tmp_path / "arguments.json"
    helper.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {arguments}\n"
                      "cat <<'EVENTS'\n" + "\n".join(json.dumps(e) for e in events) + "\nEVENTS\n")
    helper.chmod(0o755)
    return helper, arguments


def segment(speaker, start, text, id):
    return dict(type="segment", source="mic", id=id, start=start, end=start + 2, text=text, speaker_uuid=speaker)


def test_fluid_recording_stores_uuids_and_renders_registry_names(tmp_path, monkeypatch):
    speakers = tmp_path / "speakers"
    enroll(speakers, ALICE, name="Alice")
    enroll(speakers, BOB)
    helper, arguments = fluid_helper(tmp_path, [
        dict(type="ready", message="Ready"),
        segment(ALICE, 0, "Ship it on Friday.", "u1"), segment(BOB, 3, "Agreed.", "u2")])
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    code = cli.main(["--speakers-dir", str(speakers), "record", "--engine", "fluid",
                     "--output", str(tmp_path / "meetings"), "--no-refine", "--speaker-threshold", "0.8"])
    assert code == 0
    meeting = next((tmp_path / "meetings").iterdir())
    transcript = (meeting / "transcript.md").read_text()
    assert "**Alice:** Ship it on Friday." in transcript
    assert "**7c9e6679:** Agreed." in transcript
    journal = (meeting / "events.jsonl").read_text()
    assert ALICE in journal and "Alice" not in journal  # names are never journaled
    passed = arguments.read_text().split("\n")
    assert passed[passed.index("--speakers-dir") + 1] == str(speakers.resolve())
    assert passed[passed.index("--speaker-threshold") + 1] == "0.8"
    assert json.loads((meeting / "meeting.json").read_text())["engine"] == "fluid"


def test_naming_a_speaker_corrects_earlier_meetings(tmp_path, monkeypatch, capsys):
    speakers = tmp_path / "speakers"
    enroll(speakers, BOB)
    helper, _ = fluid_helper(tmp_path, [dict(type="ready", message="Ready"), segment(BOB, 0, "Agreed.", "u1")])
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    meetings = tmp_path / "meetings"
    assert cli.main(["--speakers-dir", str(speakers), "record", "--engine", "fluid",
                     "--output", str(meetings), "--no-refine"]) == 0
    transcript = next(meetings.iterdir()) / "transcript.md"
    assert "**7c9e6679:**" in transcript.read_text()
    assert cli.main(["--speakers-dir", str(speakers), "speakers", "--meetings", str(meetings),
                     "name", "7c9e", "Bob Jones"]) == 0
    assert "**Bob Jones:** Agreed." in transcript.read_text()
    assert "Corrected 1 transcript" in capsys.readouterr().out
    assert cli.main(["--speakers-dir", str(speakers), "speakers", "list"]) == 0
    assert "Bob Jones" in capsys.readouterr().out


def test_merging_and_forgetting_speakers_from_the_cli(tmp_path, capsys):
    speakers = tmp_path / "speakers"
    enroll(speakers, ALICE, name="Alice")
    enroll(speakers, BOB)
    base = ["--speakers-dir", str(speakers), "speakers", "--meetings", str(tmp_path / "meetings")]
    assert cli.main(base + ["merge", "7c9e", "0f8f"]) == 0
    registry = SpeakerRegistry(speakers)
    assert registry.resolve(BOB) == ALICE
    assert cli.main(base + ["forget", "alice"]) == 1  # no terminal, so no silent deletion
    assert "without --yes" in capsys.readouterr().err
    assert (speakers / ALICE).exists()
    assert cli.main(base + ["forget", "alice", "--yes"]) == 0
    assert not (speakers / ALICE).exists()


def test_play_without_sample_reports_a_clear_error(tmp_path, capsys):
    speakers = tmp_path / "speakers"
    enroll(speakers, ALICE)
    (speakers / ALICE / "sample.wav").unlink()
    assert cli.main(["--speakers-dir", str(speakers), "speakers", "play", "0f8f"]) == 1
    assert "no saved voice sample" in capsys.readouterr().err


@pytest.mark.parametrize("extra, message", [
    (["--locale", "fr-FR"], "English only"),
    (["--input-file", "missing.wav"], "Audio file not found"),
])
def test_fluid_rejects_unsupported_options(tmp_path, monkeypatch, capsys, extra, message):
    monkeypatch.setattr(cli, "build_helper", lambda _: tmp_path / "unused")
    code = cli.main(["--speakers-dir", str(tmp_path), "record", "--engine", "fluid",
                     "--output", str(tmp_path / "meetings"), "--no-refine", *extra])
    assert code == 1
    assert message in capsys.readouterr().err


def test_input_file_requires_the_fluid_engine(tmp_path, monkeypatch, capsys):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(cli, "build_helper", lambda _: tmp_path / "unused")
    assert cli.main(["record", "--input-file", str(audio), "--output", str(tmp_path / "meetings")]) == 1
    assert "requires --engine fluid" in capsys.readouterr().err


@pytest.mark.skipif(not os.environ.get("VOICE_NOTES_FLUID_TEST") or platform.system() != "Darwin"
                    or not shutil.which("say"),
                    reason="Set VOICE_NOTES_FLUID_TEST=1 to run the real models (downloads about 1 GB)")
def test_real_models_enroll_distinct_voices_and_recognize_them_again(tmp_path):
    import wave
    clips = []
    for index, (voice, text) in enumerate([
            ("Daniel", "Good morning everyone. Let us review the planning schedule for the next quarter."),
            ("Karen", "Thanks Daniel. The design work is finished and we are waiting for budget approval."),
            ("Daniel", "Please send the summary of today's decisions to the whole group before Friday."),
            ("Karen", "I will send it this afternoon and also schedule the follow up meeting.")]):
        aiff, wav = tmp_path / f"{index}.aiff", tmp_path / f"{index}.wav"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
        clips.append(wav)
    meeting = tmp_path / "meeting.wav"
    with wave.open(str(meeting), "wb") as out:
        out.setnchannels(1), out.setsampwidth(2), out.setframerate(16000)
        for clip in clips:
            with wave.open(str(clip)) as source:
                out.writeframes(source.readframes(source.getnframes()) + b"\0\0" * 12000)
    speakers = tmp_path / "speakers"

    def record(output):
        return cli.main(["--speakers-dir", str(speakers), "record", "--engine", "fluid", "--no-refine",
                         "--input-file", str(meeting), "--output", str(tmp_path / output), "--ui", "plain"])

    assert record("first") == 0
    registry = SpeakerRegistry(speakers)
    enrolled = registry.list()
    assert len(enrolled) == 2
    assert all(speaker["has_sample"] for speaker in enrolled)
    transcript = (next((tmp_path / "first").iterdir()) / "transcript.md").read_text()
    short_ids = {speaker["id"][:8] for speaker in enrolled}
    assert sum(transcript.count(f"**{short}:**") for short in short_ids) >= 4
    # A second recording must recognize the same two voices instead of enrolling new ones.
    assert record("second") == 0
    assert {speaker["id"] for speaker in registry.list()} == {speaker["id"] for speaker in enrolled}
