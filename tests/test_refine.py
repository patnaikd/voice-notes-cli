import asyncio
from pathlib import Path
import sys

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


def test_auth_check_finds_bundled_claude_with_minimal_environment(tmp_path, monkeypatch):
    # Under launchd PATH has no `claude` and USER may be unset; the SDK's bundled CLI must still be found,
    # and it must get USER so it can read the login from the keychain.
    package = tmp_path / "claude_agent_sdk"
    (package / "_bundled").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    cli = package / "_bundled" / "claude"
    cli.write_text('#!/bin/sh\nif [ -n "$USER" ]; then echo \'{"loggedIn": true}\'; else echo \'{"loggedIn": false}\'; fi\n')
    cli.chmod(0o755)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "USER", "LOGNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    refine.ensure_authentication()  # does not raise


def test_confirmed_login_is_cached(monkeypatch):
    import json
    from types import SimpleNamespace

    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(refine, "_login_verified", {})
    monkeypatch.setattr(refine.shutil, "which", lambda _: "/fake/claude-cached")
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps({"loggedIn": True}), returncode=0)

    monkeypatch.setattr(refine.subprocess, "run", run)
    refine.ensure_authentication()
    refine.ensure_authentication()
    assert len(calls) == 1


def _fake_cli(tmp_path):
    cli = tmp_path / "my-claude"
    cli.write_text('#!/bin/sh\necho \'{"loggedIn": true}\'\n')
    cli.chmod(0o755)
    return cli


def test_cli_override_is_used_for_login_check_and_sdk(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk
    from types import SimpleNamespace

    cli = _fake_cli(tmp_path)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(refine.CLI_ENV, str(cli))
    monkeypatch.setattr(refine, "_login_verified", {})
    monkeypatch.setattr(refine.shutil, "which", lambda _: "/should/not/be/used")
    ran = []
    original = refine.subprocess.run
    monkeypatch.setattr(refine.subprocess, "run", lambda command, **kw: ran.append(command[0]) or original(command, **kw))
    refine.ensure_authentication()
    assert ran == [str(cli.resolve())]

    seen = []

    async def query(*, prompt, options):
        seen.append(options)
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="test", result="# Notes")

    monkeypatch.setattr(sdk, "query", query)
    asyncio.run(refine._sdk_ask("Transcript", "claude-haiku-5-5"))
    assert str(seen[0].cli_path) == str(cli.resolve())


def test_sdk_keeps_bundled_cli_without_override(monkeypatch):
    import claude_agent_sdk as sdk

    monkeypatch.delenv(refine.CLI_ENV, raising=False)
    seen = []

    async def query(*, prompt, options):
        seen.append(options)
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="test", result="# Notes")

    monkeypatch.setattr(sdk, "query", query)
    asyncio.run(refine._sdk_ask("Transcript", "claude-haiku-5-5"))
    assert seen[0].cli_path is None


def test_missing_cli_override_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv(refine.CLI_ENV, str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="Claude CLI not found"):
        refine.claude_cli()


def test_cli_flag_reaches_environment_and_launchd_agent(tmp_path, monkeypatch):
    from voice_notes import cli as command
    from voice_notes.web import service

    fake = _fake_cli(tmp_path)
    monkeypatch.delenv(refine.CLI_ENV, raising=False)
    seen = []
    monkeypatch.setattr(service, "main", lambda args: seen.append(args) or 0)
    assert command.main(["--claude-cli", str(fake), "service", "install"]) == 0
    assert refine.claude_cli() == str(fake.resolve())
    args = seen[0]
    plist = service.build_plist(working_dir=tmp_path, build_dir=tmp_path,
                                environment=service.pinned_environment(args.settings, args.config_sources))
    assert Path(plist["EnvironmentVariables"][refine.CLI_ENV]).resolve() == fake.resolve()
