# Research and implementation decisions

Investigated October 10–11, 2026. The development machine reports macOS 26.6.2, arm64, and Swift 6.3.2. This describes macOS 26 APIs; newer online documentation may describe additional APIs unavailable on this machine.

## Models already provided by macOS

| Option | What it provides | How a CLI can use it | Decision |
| --- | --- | --- | --- |
| SpeechAnalyzer + SpeechTranscriber | Local speech transcription | Swift Speech framework; stream PCM through AnalyzerInput and read async results | Implemented default |
| SFSpeechRecognizer | Older Speech framework recognizer | Swift; check supportsOnDeviceRecognition and set requiresOnDeviceRecognition | Possible older-macOS backend; not implemented |
| Live Captions / Dictation | User-facing system transcription | System UI, rather than a documented transcript-export API for this CLI | Use the Speech framework instead |
| Apple Intelligence language model | Local summarization and text refinement | FoundationModels SystemLanguageModel and LanguageModelSession in Swift | Investigated; optional future formatter |

`SpeechAnalyzer` accepts an asynchronous audio sequence, with results delivered independently. One analyzer takes one sequence, so the helper creates separate analyzers for microphone and remote audio. `SpeechTranscriber` exposes availability, supported locales, installed locales, and time-indexed results. The code checks the actual supported locale instead of assuming English is available. [Apple SpeechAnalyzer](https://developer.apple.com/documentation/speech/speechanalyzer), [Apple SpeechTranscriber](https://developer.apple.com/documentation/speech/speechtranscriber).

Apple manages language assets through `AssetInventory`. They may already be installed, or need installation before first recognition; being built into the OS does not mean every language model is preloaded. The helper calls `assetInstallationRequest` and installs missing assets. It never extracts or copies private model files. [Apple AssetInventory](https://developer.apple.com/documentation/speech/assetinventory).

The older `SFSpeechRecognizer` supports on-device recognition only for supported combinations; a future fallback must explicitly require it to prevent server recognition. It is a distinct backend from SpeechAnalyzer. [Apple requiresOnDeviceRecognition](https://developer.apple.com/documentation/speech/sfspeechrecognitionrequest/requiresondevicerecognition).

Apple's local language model can summarize and refine text through `SystemLanguageModel.default` and `LanguageModelSession`. Check `availability` first: device eligibility, Apple Intelligence settings, region/language, and unfinished downloads can prevent use. For the macOS 26 system model, Apple documents a 4,096-token session context including instructions, input, and output. A meeting formatter would need small independent chunks and a careful merging strategy. It is not a speech recognizer or a replacement for Claude Agent SDK. [Apple generation guide](https://developer.apple.com/documentation/foundationmodels/generating-content-and-performing-tasks-with-foundation-models), [Apple context-window guidance](https://developer.apple.com/documentation/technotes/tn3193-managing-the-on-device-foundation-model-s-context-window).

Live Captions already processes microphone or computer audio locally on supported Apple Silicon Macs. It demonstrates built-in capability, but this implementation uses public developer APIs to get programmatic results. [Apple Live Captions](https://support.apple.com/en-gb/guide/mac-help/mchldd11f4fd/mac).

## Capturing microphone and system output

ScreenCaptureKit exposes separate audio and microphone outputs. macOS 15 added `captureMicrophone`; microphone data uses the selected device's native format, while system audio honors configured sample rate and channel count. During local diagnostics, the combined ScreenCaptureKit microphone track returned silence while standalone AVAudioEngine received nonzero microphone samples. Explicit AVAudioEngine device selection subsequently produced no frames in the combined capture test. The helper now uses AVCaptureSession with AVCaptureDeviceInput bound directly to the selected microphone's UID. Core Audio enumerates inputs and outputs for the CLI selector and display. ScreenCaptureKit captures only remote/system audio, with a minimal screen sink whose samples are discarded immediately. Each track is converted independently and retains a shared presentation-time origin. Screen/system capture still requires macOS privacy authorization. [Apple SCStreamConfiguration](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration), [Apple SCStreamOutputType](https://developer.apple.com/documentation/screencapturekit/scstreamoutputtype).

All-system capture is the default. Optional application filtering uses `SCContentFilter` with a running application's bundle ID. Browser filters include other audible tabs, and helper-process attribution can vary. There is no automatic meeting lifecycle detection, participant-name integration, or acoustic echo cancellation. [Apple SCContentFilter](https://developer.apple.com/documentation/screencapturekit/sccontentfilter).

The HyperX USB microphone exposes a multichannel/discrete layout. Native samples were nonzero while automatic conversion to mono produced zeros. Explicitly mapping channel 0 restored the microphone signal. A synthetic regression reproduces the zero output with the original converter and nonzero output with the fix; live microphone and system speech were subsequently confirmed.

## Moonshine investigation

Moonshine Voice is aimed at local streaming recognition and has Python and native macOS bindings. Its base `Transcriber` accepts mono PCM through `start`, `add_audio`, and `stop`, with callbacks for partial and completed lines. Unlike microphone-only examples, this app feeds two captured tracks into independent recognizers. The Python package is pinned to **0.1.5**, and its installed source was inspected before integrating. [Moonshine transcription guide](https://moonshine-voice.readthedocs.io/en/latest/using/transcription/), [Moonshine project](https://github.com/moonshine-ai/moonshine).

`identify_speakers` enables diarization, requires additional model assets, and implies word timestamps. Direct Transcriber construction must supply `diarization_model_dir`; the app downloads it explicitly. Recent completed lines can receive `LineSpeakersChanged` events as clustering improves. The journal therefore records updates and renders the latest version. Anonymous clusters do not identify a real person or retrieve a Zoom display name. [Moonshine options](https://moonshine-voice.readthedocs.io/en/stable/api/options/), [Moonshine diarization assets](https://github.com/moonshine-ai/moonshine/blob/main/docs/diarization-models.md).

The recommended language model is selected by Moonshine's installed catalog. Models are downloaded once and reused. Upstream licensing differs between streaming models and some legacy language models; the pinned package emits a licensing message for non-English selections. Check the selected model's license before distribution. CPU inference is the upstream default; we have not measured CPU, memory, latency, transcription error, or diarization error on this machine. A smoke test fed the first 12 seconds of Moonshine's bundled Beckett sample through the app's streaming adapter with diarization: finalized text and speaker spans were emitted successfully. This is functional validation, not an accuracy benchmark. [Moonshine license](https://github.com/moonshine-ai/moonshine/blob/main/LICENSE), [Moonshine model documentation](https://moonshine-voice.readthedocs.io/en/stable/models/).

## Haiku refinement

The app uses `claude_agent_sdk.query` with `ClaudeAgentOptions(model="claude-haiku-5-5")`. Both recording and recovery use this explicit model ID by default; `--model` can override it. The SDK bundles the Claude Code CLI. It runs with `tools=[]`, empty setting sources, strict MCP configuration, and an empty temporary working directory. `allowed_tools=[]` by itself is insufficient to remove tools, so it is not used as the isolation mechanism. [Anthropic Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python), [official SDK repository](https://github.com/anthropics/claude-agent-sdk-python).

Formatting operates on saved finalized text after recording. The prompt asks for conservative editing and states that missing owners, deadlines, and decisions must not be invented. Long transcripts are split into bounded chunks, each output is checkpointed, and the final file retains each part. This avoids a single context overflow, but means decisions repeated across parts are not globally deduplicated. Text goes to Claude; raw audio does not. The CLI loads a local `.env` without overriding the shell and checks credentials before SDK startup. An API-key-authenticated request to `claude-haiku-5-5` and formatting of a saved transcript completed successfully. Formatting startup and requests are bounded by an isolated worker deadline; matching completed parts can be reused on retry.

## Validation and remaining work

Validated on macOS 26: local microphone and system transcription, independent timestamps, the HyperX channel mapping fix, and authenticated Haiku formatting. Permission checks must be verified from a normal terminal: sandboxed diagnostics can misleadingly report unavailable assets or denied audio access.

Still to measure: recognition accuracy, CPU/memory use, diarization accuracy, long-meeting performance, and capture through different meeting apps/devices. Speaker IDs remain anonymous. Native capture requires real-device checks beyond mocked CLI tests; CI does not record audio or make paid model requests.

Whisper/Core ML or whisper.cpp remains another local ASR option, but requires separately downloaded weights and has not been integrated. For older macOS, a legacy Speech backend or audio driver capture would need separate implementation rather than a claim that this macOS 26 helper works unchanged.
