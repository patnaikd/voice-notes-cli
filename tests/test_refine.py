import asyncio

import pytest

from voice_notes import refine


def test_long_paragraphs_are_bounded_without_losing_text():
    text = "a" * 101
    parts = refine.chunks(text, limit=20)
    assert all(len(part) <= 20 for part in parts)
    assert "".join(parts) == text


def test_chunk_failure_keeps_completed_checkpoint(tmp_path, monkeypatch):
    calls = []

    async def ask(text, model):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("Authentication failed")
        return "# Summary\nApproved.\n"

    monkeypatch.setattr(refine, "ask_haiku", ask)
    with pytest.raises(RuntimeError, match="Authentication"):
        asyncio.run(refine.refine("a" * 25000, "claude-haiku-5-5", tmp_path))
    assert (tmp_path / "notes-part-001.md").read_text() == "# Summary\nApproved.\n"
    assert not (tmp_path / "notes.md").exists()


def test_agent_options_remove_tools_and_settings(monkeypatch):
    import claude_agent_sdk as sdk

    seen = []

    async def query(*, prompt, options):
        seen.append(options)
        assert "meeting transcript data" in prompt
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="test", result="# Notes")

    monkeypatch.setattr(sdk, "query", query)
    assert asyncio.run(refine._sdk_ask("We approved this.", "claude-haiku-5-5")) == "# Notes\n"
    assert seen[0].model == "claude-haiku-5-5"
    assert seen[0].tools == []
    assert seen[0].setting_sources == []
    assert seen[0].strict_mcp_config


def test_agent_error_does_not_return_successful_notes(monkeypatch):
    import claude_agent_sdk as sdk

    async def query(**kwargs):
        yield sdk.ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                                is_error=True, num_turns=1, session_id="test", result="partial")

    monkeypatch.setattr(sdk, "query", query)
    with pytest.raises(RuntimeError, match="formatting failed"):
        asyncio.run(refine._sdk_ask("Transcript", "claude-haiku-5-5"))


def test_missing_authentication_fails_before_sdk_import(monkeypatch):
    import json
    from types import SimpleNamespace

    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CODE_USE_ANTHROPIC_AWS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(refine.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(refine.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout=json.dumps({"loggedIn": False}), returncode=1))
    with pytest.raises(RuntimeError, match="not authenticated"):
        asyncio.run(refine.ask_haiku("Transcript", "claude-haiku-5-5"))


def test_startup_timeout_kills_worker(monkeypatch):
    import sys

    spawned = []
    original = asyncio.create_subprocess_exec

    async def stalled_worker(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(refine, "ensure_authentication", lambda: None)
    monkeypatch.setattr(refine, "REQUEST_TIMEOUT", 0.1)
    monkeypatch.setattr(refine.asyncio, "create_subprocess_exec", stalled_worker)
    with pytest.raises(RuntimeError, match="timed out.*SDK startup"):
        asyncio.run(refine.ask_haiku("Transcript", "claude-haiku-5-5"))
    assert spawned[0].returncode is not None


def test_isolated_worker_returns_notes_with_fake_sdk(tmp_path, monkeypatch):
    fake = tmp_path / "claude_agent_sdk.py"
    fake.write_text('''
class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        assert kwargs["model"] == "claude-haiku-5-5"
        assert kwargs["tools"] == []
        assert kwargs["setting_sources"] == []
class ResultMessage:
    is_error = False
    subtype = "success"
    result = "# Notes\\nThe original text was preserved."
async def query(*, prompt, options):
    assert "Original meeting text" in prompt
    yield ResultMessage()
''')
    monkeypatch.setattr(refine, "ensure_authentication", lambda: None)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    notes = asyncio.run(refine.ask_haiku("Original meeting text", "claude-haiku-5-5"))
    assert notes == "# Notes\nThe original text was preserved.\n"


def test_retry_reuses_matching_completed_parts(tmp_path, monkeypatch):
    calls = []
    async def ask(text, model):
        calls.append((text, model))
        if len(calls) == 2:
            raise RuntimeError("Connection lost")
        return "# Notes\n" + text[:3] + "\n"
    monkeypatch.setattr(refine, "ask_haiku", ask)
    text = "a" * 25000
    with pytest.raises(RuntimeError, match="Connection lost"):
        asyncio.run(refine.refine(text, "model-one", tmp_path))
    notes = asyncio.run(refine.refine(text, "model-one", tmp_path))
    assert len(calls) == 3
    assert len(calls[-1][0]) == 1000
    assert "Part 2" in notes
    asyncio.run(refine.refine(text, "model-two", tmp_path))
    assert len(calls) == 5


def test_modified_checkpoint_is_regenerated(tmp_path, monkeypatch):
    calls = []
    async def ask(text, model):
        calls.append(text)
        return "# Notes\nVerified.\n"
    monkeypatch.setattr(refine, "ask_haiku", ask)
    asyncio.run(refine.refine("Original", "model", tmp_path))
    (tmp_path / "notes-part-001.md").write_text("Incomplete or changed")
    asyncio.run(refine.refine("Original", "model", tmp_path))
    assert len(calls) == 2
