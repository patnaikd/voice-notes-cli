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
        asyncio.run(refine.refine("a" * 25000, "haiku", tmp_path))
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
    assert asyncio.run(refine.ask_haiku("We approved this.", "haiku")) == "# Notes\n"
    assert seen[0].model == "haiku"
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
        asyncio.run(refine.ask_haiku("Transcript", "haiku"))
