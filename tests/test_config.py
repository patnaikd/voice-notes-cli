"""~/.voice-notes-config.json: precedence, `configure`, the server's use of it, and the app launcher."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

from voice_notes import cli, config, launcher


def saved(path):
    return json.loads(Path(path).read_text())


def test_precedence_is_flag_then_env_then_file_then_default(isolated_config, monkeypatch):
    config.save({"model": "from-file", "locale": "de-DE", "port": 9001})
    monkeypatch.setenv("VOICE_NOTES_LOCALE", "fr-FR")
    values, sources = config.resolve({"port": 9100})
    assert (values["port"], sources["port"]) == (9100, "flag")
    assert (values["locale"], sources["locale"]) == ("fr-FR", "env")
    assert (values["model"], sources["model"]) == ("from-file", "config")
    assert (values["source"], sources["source"]) == ("both", "default")


def test_record_reads_the_config_file_on_every_launch(isolated_config, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "record", seen.append)
    config.save({"meetings_dir": str(tmp_path / "meetings"), "engine": "fluid", "model": "claude-opus-5-5"})
    assert cli.main(["record"]) == 0
    args = seen[-1]
    assert (args.output, args.engine, args.model, args.locale) == (
        tmp_path / "meetings", "fluid", "claude-opus-5-5", "en-US")
    assert cli.main(["record", "--engine", "apple", "--output", "elsewhere"]) == 0
    assert (seen[-1].engine, seen[-1].output) == ("apple", Path("elsewhere"))
    config.save({"engine": None})
    assert cli.main(["record"]) == 0
    assert seen[-1].engine == "apple"  # record's own default when nothing is configured


def test_meetings_folder_applies_to_every_command(isolated_config, tmp_path, monkeypatch):
    config.save({"meetings_dir": str(tmp_path / "m")})
    seen = []
    monkeypatch.setattr(cli, "manage_speakers", seen.append)
    assert cli.main(["speakers", "list"]) == 0
    assert seen[0].meetings == tmp_path / "m"


def test_configure_set_and_unset_are_scriptable(isolated_config, tmp_path, capsys):
    tool = tmp_path / "claude"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    assert cli.main(["configure", "--set", "engine=fluid", "--set", f"claude-cli={tool}",
                     "--set", "meetings_dir=~/vn-meetings", "--set", "port=9000"]) == 0
    data = saved(isolated_config)
    assert data == {"meetings_dir": str(Path("~/vn-meetings").expanduser()), "engine": "fluid",
                    "claude_cli": str(tool.resolve()), "port": 9000}
    output = capsys.readouterr().out
    assert "engine        fluid  (from the config file)" in output
    assert cli.main(["configure", "--unset", "engine"]) == 0
    assert "engine" not in saved(isolated_config)


@pytest.mark.parametrize("assignment", ["engine=whisper", "port=0", "colour=blue", "claude_cli=/no/such/claude"])
def test_configure_rejects_bad_values_without_saving(isolated_config, assignment, capsys):
    assert cli.main(["configure", "--set", assignment]) == 1
    assert not isolated_config.exists()
    assert "voice-notes:" in capsys.readouterr().err


def test_configure_interactive_keeps_defaults_and_pins_meetings_folder(isolated_config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save({"model": "claude-sonnet-5-5", "mic": "Old mic"})
    # One answer per option, in order: keep, apple, keep, keep, reset mic, keep, keep, keep, keep.
    answers = iter(["", "whisper", "apple", "", "", "-", "", "", "", ""])
    changes = config.interactive(ask=lambda prompt: next(answers), say=lambda *a: None)
    assert changes == {"meetings_dir": str(tmp_path / "meetings"), "engine": "apple", "mic": None}
    config.save(changes)
    assert saved(isolated_config) == {"meetings_dir": str(tmp_path / "meetings"), "engine": "apple",
                                      "model": "claude-sonnet-5-5"}


def test_bad_saved_values_are_reported_and_ignored(isolated_config, capsys):
    isolated_config.write_text(json.dumps({"engine": "whisper", "model": "m", "future_key": 1}))
    values, sources = config.resolve()
    assert values["engine"] is None and values["model"] == "m"
    assert "ignoring engine" in capsys.readouterr().err
    config.save({"locale": "en-GB"})
    assert saved(isolated_config)["future_key"] == 1  # unknown keys survive a save


def test_unreadable_config_does_not_stop_configure(isolated_config, capsys):
    isolated_config.write_text("{not json")
    assert cli.main(["configure", "--show"]) == 0
    assert "ignoring" in capsys.readouterr().err


def test_server_uses_configured_settings_and_writes_browser_changes_back(isolated_config, tmp_path):
    from fastapi.testclient import TestClient
    from voice_notes.web.app import Config, Server, create_app

    config.save({"engine": "apple", "model": "from-config", "locale": "en-GB"})
    values, sources = config.resolve()
    server = Server(Config(data_dir=tmp_path / "data", fake_llm=True, config_file=isolated_config,
                           settings_overrides={k: values[k] for k in ("engine", "source", "mic", "locale", "model")
                                               if sources[k] != "default"}))
    assert server.store.settings["engine"] == "apple" and server.store.settings["locale"] == "en-GB"
    with TestClient(create_app(server)) as client:
        current = client.get("/api/settings").json()
        response = client.put("/api/settings", json={**current, "model": "from-browser", "segment_minutes": 15})
        assert response.status_code == 200
    data = saved(isolated_config)
    assert data["model"] == "from-browser" and "segment_minutes" not in data and data["engine"] == "apple"


def test_launcher_app_bundle(isolated_config, tmp_path):
    app = launcher.install(tmp_path / "Applications", build_dir=tmp_path / ".build", working_dir=tmp_path)
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    script = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
    assert app.name == "Voice Notes.app" and os.access(script, os.X_OK)
    text = script.read_text()
    assert sys.executable in text and "launcher open" in text and str(isolated_config) in text
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
    assert launcher.uninstall(tmp_path / "Applications") == app and not app.exists()
    foreign = tmp_path / "Other" / "Voice Notes.app"
    foreign.mkdir(parents=True)
    assert launcher.uninstall(tmp_path / "Other") is None and foreign.exists()


def test_launcher_reuses_a_running_server(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher, "running", lambda port, timeout=1.0: True)
    monkeypatch.setattr(launcher, "open_browser", opened.append)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start a server"))
    launcher.open_ui(port=9123, data_dir=Path("m"), build_dir=Path(".build"), speakers_dir=Path("s"))
    assert opened == ["http://localhost:9123/"]


def test_launcher_starts_the_server_and_opens_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    opened, polls = [], iter([False, False, True])
    monkeypatch.setattr(launcher, "running", lambda port, timeout=1.0: next(polls))
    monkeypatch.setattr(launcher, "open_browser", opened.append)
    launcher.open_ui(port=9124, data_dir=Path("m"), build_dir=Path(".build"), speakers_dir=Path("s"),
                     command=[sys.executable, "-c", "import time; time.sleep(5)"])
    assert opened == ["http://localhost:9124/"]
    assert (tmp_path / "Library" / "Logs" / "voice-notes" / "server.launcher.log").is_file()


def test_launcher_reports_a_server_that_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(launcher, "running", lambda port, timeout=1.0: False)
    with pytest.raises(RuntimeError, match="exited with status 3"):
        launcher.open_ui(port=9125, data_dir=Path("m"), build_dir=Path(".build"), speakers_dir=Path("s"),
                         command=[sys.executable, "-c", "print('port in use'); raise SystemExit(3)"], timeout=10)
