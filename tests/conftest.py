"""Keep every test away from the developer's own ~/.voice-notes-config.json and VOICE_NOTES_* variables."""
import pytest

from voice_notes import config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("config") / "voice-notes-config.json"
    for option in config.OPTIONS:
        monkeypatch.delenv(option.env, raising=False)
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    return path
