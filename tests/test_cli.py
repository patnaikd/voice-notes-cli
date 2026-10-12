import json

import pytest

from voice_notes import cli


def test_format_failure_preserves_readable_transcript(tmp_path, monkeypatch):
    journal = tmp_path / "events.jsonl"
    journal.write_text(json.dumps(dict(type="segment", source="mic", id="1", start=0,
                                      end=1, text="I will send the report.")) + "\n")

    async def fail(*args):
        raise RuntimeError("No credentials")

    monkeypatch.setattr(cli, "refine", fail)
    result = cli.main(["refine", str(journal), "--title", "Weekly planning"])
    assert result == 1
    assert "I will send the report." in (tmp_path / "transcript.md").read_text()
    assert not (tmp_path / "notes.md").exists()


def test_duration_rejects_nan():
    with pytest.raises(SystemExit):
        cli.main(["record", "--duration", "nan"])


def test_capture_failure_preserves_finalized_speech(tmp_path, monkeypatch):
    helper = tmp_path / "fake-capture"
    # Exercise the real subprocess and journal paths without using devices.
    events = [dict(type="level", source="mic", rms=0.1, peak=0.4),
              dict(type="segment", source="mic", id="1", start=0, end=1,
                   text="Save this sentence."),
              dict(type="error", message="Capture disconnected")]
    helper.write_text("#!/bin/sh\ncat <<'EVENTS'\n" +
                      "\n".join(json.dumps(event) for event in events) + "\nEVENTS\n")
    helper.chmod(0o755)
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    code = cli.main(["record", "--output", str(tmp_path / "meetings"), "--no-refine"])
    assert code == 1
    transcript = next((tmp_path / "meetings").glob("*/transcript.md"))
    assert "Save this sentence." in transcript.read_text()
    assert json.loads(transcript.with_name("meeting.json").read_text())["app"] == "all"
    assert '"type": "level"' not in transcript.with_name("events.jsonl").read_text()


def test_permission_failure_does_not_suggest_refining_empty_transcript(tmp_path, monkeypatch, capsys):
    helper = tmp_path / "fake-capture"
    event = dict(type="error", code="screen_permission_denied", message="Screen/system audio permission denied")
    helper.write_text("#!/bin/sh\ncat <<'EVENTS'\n" + json.dumps(event) + "\nEVENTS\nexit 1\n")
    helper.chmod(0o755)
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    assert cli.main(["record", "--output", str(tmp_path / "meetings")]) == 1
    error = capsys.readouterr().err
    assert "No speech was captured" in error
    assert "Recover" not in error


def test_devices_distinguishes_inputs_outputs_and_defaults(tmp_path, monkeypatch, capsys):
    helper = tmp_path / "fake-devices"
    payload = dict(type="devices", devices=[
        dict(name="USB Microphone", uid="input-uid", input_channels=2, output_channels=0,
             default_input=True, default_output=False),
        dict(name="Headset", uid="output-uid", input_channels=0, output_channels=2,
             default_input=False, default_output=True),
    ])
    helper.write_text("#!/bin/sh\ncat <<'DEVICES'\n" + json.dumps(payload) + "\nDEVICES\n")
    helper.chmod(0o755)
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    assert cli.main(["devices"]) == 0
    output = capsys.readouterr().out
    inputs, outputs = output.split("Output devices:")
    assert "USB Microphone [system default]" in inputs
    assert "Headset" not in inputs
    assert "Headset [system default]" in outputs
    assert "USB Microphone" not in outputs
