# Bundled Fluid engine models

`models/fluid/` holds the CoreML models that `--engine fluid` (and the web UI's default engine) loads, so a clone can transcribe without downloading them. The files are stored with [Git LFS](https://git-lfs.com/) because several exceed GitHub's 100 MB file limit.

```sh
brew install git-lfs
git lfs install --local   # once per clone
git lfs pull              # fetch the model files (about 850 MB)
```

## How they are found

The CLI and the web server pass `--models-dir` to the capture helper:

1. `VOICE_NOTES_MODELS`, if set.
2. Otherwise `models/fluid` in this repository.

If the folder is missing, or still holds unfetched Git LFS pointer files, the helper falls back to FluidAudio's download cache (`~/Library/Application Support/FluidAudio/Models`) and downloads from Hugging Face on first use. Any file missing from `models/fluid` is downloaded into that folder.

## Contents and licenses

| Folder | Source | Used for | License |
| --- | --- | --- | --- |
| `parakeet-unified-en-0.6b/` (streaming encoder, decoder, joint, vocab) | [FluidInference/parakeet-unified-en-0.6b-coreml](https://huggingface.co/FluidInference/parakeet-unified-en-0.6b-coreml), converted from `nvidia/parakeet-unified-en-0.6b` | Speech recognition | CC-BY-4.0 (CoreML conversion); upstream NVIDIA Open Model License |
| `sortformer/` | [FluidInference/diar-streaming-sortformer-coreml](https://huggingface.co/FluidInference/diar-streaming-sortformer-coreml), converted from `nvidia/diar_streaming_sortformer_4spk-v2.1` | Speaker diarization | CC-BY-4.0 (CoreML conversion); upstream NVIDIA Open Model License |
| `campplus/` | [FluidInference/campplus-coreml](https://huggingface.co/FluidInference/campplus-coreml), from ModelScope [FunASR](https://github.com/modelscope/FunASR) CAM++ | Voice embeddings for named speakers | "other": see the FunASR model license |

The non-streaming Parakeet encoder (`parakeet_unified_encoder_int8.mlmodelc`, 567 MB) is not included; the live engine does not use it.

## Updating

The files must match the FluidAudio version pinned in `src/voice_notes/native/Package.swift`. After changing that pin, let the helper download fresh models once (for example with `VOICE_NOTES_MODELS=/nonexistent`), then copy them in:

```sh
SRC="$HOME/Library/Application Support/FluidAudio/Models"
rsync -a --delete "$SRC/campplus" "$SRC/sortformer" models/fluid/
rsync -a --delete --exclude 'parakeet_unified_encoder_int8.mlmodelc' "$SRC/parakeet-unified-en-0.6b" models/fluid/
```
