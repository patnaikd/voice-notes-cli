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


def test_malformed_capture_event_preserves_previous_speech(tmp_path, monkeypatch, capsys):
    helper = tmp_path / "fake-capture"
    event = dict(type="segment", source="mic", id="1", start=0, end=1, text="Keep this.")
    helper.write_text("#!/bin/sh\ncat <<'EVENTS'\n" + json.dumps(event) + "\n[]\nEVENTS\n")
    helper.chmod(0o755)
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    assert cli.main(["record", "--output", str(tmp_path / "meetings"), "--no-refine"]) == 1
    transcript = next((tmp_path / "meetings").glob("*/transcript.md"))
    assert "Keep this." in transcript.read_text()
    assert "Invalid capture event" in capsys.readouterr().err


def test_startup_timeout_finalizes_and_preserves_speech(tmp_path, monkeypatch, capsys):
    helper = tmp_path / "fake-capture"
    helper.write_text('''#!/bin/sh
exec python3 - <<'SCRIPT'
import json, signal, time, sys
def finish(*args):
    print(json.dumps(dict(type="segment", source="mic", id="1", start=0, end=1, text="Final speech.")), flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, finish)
print(json.dumps(dict(type="status", message="Starting")), flush=True)
while True: time.sleep(0.01)
SCRIPT
''')
    helper.chmod(0o755)
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    assert cli.main(["record", "--output", str(tmp_path / "meetings"),
                     "--startup-timeout", "0.5", "--no-refine"]) == 1
    assert "Final speech." in next((tmp_path / "meetings").glob("*/transcript.md")).read_text()
    assert "did not become ready" in capsys.readouterr().err


def mac(monkeypatch, version="26.1", machine="arm64"):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: (version, ("", "", ""), machine))
    monkeypatch.setattr(cli.platform, "machine", lambda: machine)


@pytest.mark.parametrize("status, expected", [
    (dict(status="available"), "on-device model ready"),
    (dict(status="unavailable", reason="appleIntelligenceNotEnabled"),
     "Turn on Apple Intelligence in System Settings > Apple Intelligence & Siri"),
    (dict(status="unavailable", reason="modelNotReady"), "still downloading"),
    (dict(status="unavailable", reason="deviceNotEligible"), "needs Apple silicon"),
    (dict(status="unavailable", reason="somethingNew"), "unavailable (somethingNew)"),
    (dict(status="unsupported"), "not supported"),
    (None, "not supported"),
])
def test_apple_intelligence_reasons_have_plain_fixes(monkeypatch, status, expected):
    mac(monkeypatch)
    assert expected in cli.apple_intelligence_line(status)


@pytest.mark.parametrize("system, version, machine", [
    ("Linux", "", "x86_64"), ("Darwin", "15.5", "arm64"), ("Darwin", "26.1", "x86_64")])
def test_apple_intelligence_not_supported_on_old_macos_or_intel(monkeypatch, system, version, machine):
    mac(monkeypatch, version, machine)
    monkeypatch.setattr(cli.platform, "system", lambda: system)
    line = cli.apple_intelligence_line(dict(status="available"))
    assert line.startswith("Apple Intelligence: not supported")


def test_doctor_reports_apple_intelligence(tmp_path, monkeypatch, capsys):
    mac(monkeypatch)
    helper = tmp_path / "fake-capture"
    event = dict(type="doctor", apple_available=True, microphone_permission=3,
                 apple_intelligence=dict(status="unavailable", reason="appleIntelligenceNotEnabled"))
    helper.write_text(f"#!/bin/sh\necho '{json.dumps(event)}'\n")
    helper.chmod(0o755)
    monkeypatch.setattr(cli, "build_helper", lambda _: helper)
    assert cli.main(["--speakers-dir", str(tmp_path / "speakers"), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "Apple Intelligence: turned off. Turn on Apple Intelligence" in out
    assert '"apple_intelligence"' not in out
    assert '"microphone_permission": 3' in out


def test_doctor_says_not_supported_before_helper_fails_on_old_macos(tmp_path, monkeypatch, capsys):
    mac(monkeypatch, "15.5")

    def old_macos(_):
        raise RuntimeError("This helper requires macOS 26+.")

    monkeypatch.setattr(cli, "build_helper", old_macos)
    assert cli.main(["--speakers-dir", str(tmp_path / "speakers"), "doctor"]) == 1
    assert "Apple Intelligence: not supported (needs macOS 26" in capsys.readouterr().out
