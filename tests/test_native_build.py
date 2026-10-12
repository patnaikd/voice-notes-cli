import os
import subprocess
from types import SimpleNamespace

import pytest

from voice_notes import native


@pytest.mark.parametrize("failure", ["compiler", "timeout"])
def test_failed_build_preserves_existing_helper(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(native.platform, "mac_ver", lambda: ("26.0", (), ""))
    binary = tmp_path / "voice-notes-capture"
    binary.write_bytes(b"existing helper")
    os.utime(binary, (1, 1))
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return SimpleNamespace(returncode=1, stderr="Compiler failure")
    monkeypatch.setattr(native.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        native.build_helper(tmp_path)
    assert binary.read_bytes() == b"existing helper"
    assert not list(tmp_path.glob(".compile-*"))
