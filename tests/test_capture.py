from pathlib import Path

from voice_notes import capture
from voice_notes.capture import fluid_models_dir, helper_arguments


def make_models(root: Path, content: bytes = b"\x00weights") -> Path:
    weights = root / "parakeet-unified-en-0.6b" / "encoder.mlmodelc" / "weights"
    weights.mkdir(parents=True)
    (weights / "weight.bin").write_bytes(content)
    return root


def test_models_dir_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_MODELS", str(make_models(tmp_path / "models")))
    assert fluid_models_dir() == (tmp_path / "models").resolve()


def test_missing_models_dir_falls_back_to_download_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_MODELS", str(tmp_path / "absent"))
    assert fluid_models_dir() is None


def test_repository_models_are_used_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_NOTES_MODELS", raising=False)
    monkeypatch.setattr(capture, "REPO_MODELS", make_models(tmp_path / "fluid"))
    assert fluid_models_dir() == tmp_path / "fluid"


def test_unfetched_lfs_pointers_are_not_used(tmp_path, monkeypatch, capsys):
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 590000000\n"
    monkeypatch.setenv("VOICE_NOTES_MODELS", str(make_models(tmp_path / "models", pointer)))
    assert fluid_models_dir() is None
    assert "git lfs pull" in capsys.readouterr().err


def test_models_dir_is_passed_only_to_the_fluid_engine(tmp_path):
    fluid = helper_arguments(engine="fluid", source="mic", speakers_dir=tmp_path, models_dir=tmp_path / "m")
    assert fluid[fluid.index("--models-dir") + 1] == str((tmp_path / "m").resolve())
    assert "--models-dir" not in helper_arguments(engine="fluid", source="mic", speakers_dir=tmp_path)
    assert "--models-dir" not in helper_arguments(engine="apple", source="mic", models_dir=tmp_path / "m")
