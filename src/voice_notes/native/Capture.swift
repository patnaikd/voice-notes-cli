import Foundation
import AVFoundation
import ScreenCaptureKit
import Speech
import CoreMedia
import CoreAudio
import AudioToolbox

struct AudioDeviceInfo {
    let id: AudioDeviceID
    let uid: String
    let name: String
    let inputChannels: Int
    let outputChannels: Int
    var json: [String: Any] {
        ["id": id, "uid": uid, "name": name, "input_channels": inputChannels,
         "output_channels": outputChannels,
         "default_input": id == AudioDevices.defaultDevice(kAudioHardwarePropertyDefaultInputDevice),
         "default_output": id == AudioDevices.defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)]
    }
}

enum AudioDevices {
    static func address(_ selector: AudioObjectPropertySelector,
                        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }
    static func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioDeviceID {
        var property = address(selector), value = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &property, 0, nil, &size, &value)
        return value
    }
    static func string(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String {
        var property = address(selector), value: CFString = "" as CFString
        var size = UInt32(MemoryLayout<CFString>.size)
        guard AudioObjectGetPropertyData(id, &property, 0, nil, &size, &value) == noErr else { return "Unknown" }
        return value as String
    }
    static func channels(_ id: AudioDeviceID, scope: AudioObjectPropertyScope) -> Int {
        var property = address(kAudioDevicePropertyStreamConfiguration, scope: scope)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &property, 0, nil, &size) == noErr, size > 0 else { return 0 }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(id, &property, 0, nil, &size, raw) == noErr else { return 0 }
        let buffers = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return buffers.reduce(0) { $0 + Int($1.mNumberChannels) }
    }
    static func all() throws -> [AudioDeviceInfo] {
        var property = address(kAudioHardwarePropertyDevices), size: UInt32 = 0
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &property, 0, nil, &size) == noErr else {
            throw Failure(message: "Cannot enumerate audio devices")
        }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        let result = ids.withUnsafeMutableBytes { bytes in
            AudioObjectGetPropertyData(system, &property, 0, nil, &size, bytes.baseAddress!)
        }
        guard result == noErr else { throw Failure(message: "Cannot read audio devices (\(result))") }
        return ids.map { id in
            AudioDeviceInfo(id: id, uid: string(id, kAudioDevicePropertyDeviceUID),
                name: string(id, kAudioObjectPropertyName),
                inputChannels: channels(id, scope: kAudioObjectPropertyScopeInput),
                outputChannels: channels(id, scope: kAudioObjectPropertyScopeOutput))
        }
    }
    static func microphone(_ selection: String) throws -> AudioDeviceInfo {
        let devices = try all().filter { $0.inputChannels > 0 }
        let matches = devices.filter {
            selection.isEmpty ? $0.id == defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
                : $0.uid == selection || String($0.id) == selection || $0.name.caseInsensitiveCompare(selection) == .orderedSame
        }
        guard matches.count == 1 else {
            throw Failure(message: "Microphone '\(selection.isEmpty ? "system default" : selection)' is missing or ambiguous. Run voice-notes devices and choose --mic using its name or UID.")
        }
        return matches[0]
    }
}

func emit(_ object: [String: Any]) {
    // All writes are serialized, including asynchronous speech results.
    Output.lock.lock()
    defer { Output.lock.unlock() }
    if let data = try? JSONSerialization.data(withJSONObject: object) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}
enum Output { static let lock = NSLock() }
struct Failure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

// Accessed only from the capture's serial queue.
final class Converter {
    let target: AVAudioFormat
    var converter: AVAudioConverter?
    init(target: AVAudioFormat) { self.target = target }
    func convert(_ input: AVAudioPCMBuffer) throws -> AVAudioPCMBuffer {
        if input.format == target { return input }
        if converter?.inputFormat != input.format {
            converter = AVAudioConverter(from: input.format, to: target)
            // USB microphones can report discrete channels that the default
            // stereo-to-mono matrix discards. Select channel 0 explicitly for
            // mono speech input instead of relying on the inferred layout.
            if target.channelCount == 1 && input.format.channelCount > 1 {
                converter?.channelMap = [0]
            }
        }
        guard let converter else { throw Failure(message: "Unsupported audio format") }
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * target.sampleRate / input.format.sampleRate)) + 32
        let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity)!
        var supplied = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true
            status.pointee = .haveData
            return input
        }
        if let error { throw error }
        return output
    }
}

final class AppleTrack {
    let source: String
    let analyzer: SpeechAnalyzer
    let continuation: AsyncStream<AnalyzerInput>.Continuation
    let converter: Converter
    var nextAudioTime: CMTime?
    var resultsTask: Task<Void, Never>?
    private init(source: String, analyzer: SpeechAnalyzer,
                 continuation: AsyncStream<AnalyzerInput>.Continuation, format: AVAudioFormat) {
        self.source = source
        self.analyzer = analyzer
        self.continuation = continuation
        self.converter = Converter(target: format)
    }
    static func create(source: String, locale: Locale) async throws -> AppleTrack {
        guard SpeechTranscriber.isAvailable,
              let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw Failure(message: "Apple SpeechTranscriber is unavailable for \(locale.identifier); try --engine moonshine")
        }
        let transcriber = SpeechTranscriber(locale: supported,
            transcriptionOptions: [], reportingOptions: [.volatileResults], attributeOptions: [.audioTimeRange])
        if await AssetInventory.status(forModules: [transcriber]) != .installed,
           let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            emit(["type": "status", "message": "Installing Apple's speech assets for \(supported.identifier)"])
            try await request.downloadAndInstall()
        }
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw Failure(message: "No compatible Apple speech audio format")
        }
        let (inputs, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let track = AppleTrack(source: source, analyzer: analyzer, continuation: continuation, format: format)
        track.resultsTask = Task {
            do {
                for try await result in transcriber.results {
                    let start = max(0, CMTimeGetSeconds(result.range.start))
                    emit(["type": result.isFinal ? "segment" : "partial", "source": source,
                          "id": "\(result.range.start.value)/\(result.range.start.timescale)",
                          "start": start, "end": start + CMTimeGetSeconds(result.range.duration),
                          "text": String(result.text.characters)])
                }
            } catch { emit(["type": "error", "message": "\(source) recognition: \(error.localizedDescription)"]) }
        }
        try await analyzer.prepareToAnalyze(in: format)
        try await analyzer.start(inputSequence: inputs)
        return track
    }
    func add(_ buffer: AVAudioPCMBuffer, at seconds: Double) throws {
        let converted = try converter.convert(buffer)
        if converted.frameLength > 0 {
            // Resampling and host-clock rounding can put adjacent buffers a few
            // samples apart. SpeechAnalyzer rejects even tiny overlaps.
            let scale = Int32(converter.target.sampleRate)
            var start = CMTime(seconds: seconds, preferredTimescale: scale)
            if let nextAudioTime, CMTimeCompare(start, nextAudioTime) < 0 { start = nextAudioTime }
            nextAudioTime = CMTimeAdd(start, CMTime(value: Int64(converted.frameLength), timescale: scale))
            continuation.yield(AnalyzerInput(buffer: converted,
                bufferStartTime: start))
        }
    }
    func finish() async throws {
        continuation.finish()
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        await resultsTask?.value
    }
}

struct LevelReading {
    var sum: Double = 0
    var count: Int = 0
    var peak: Float = 0
    var emittedAt: Double = 0
}

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate, AVCaptureAudioDataOutputSampleBufferDelegate {
    let queue = DispatchQueue(label: "voice-notes.audio")
    let tracks: [String: AppleTrack]
    let converters: [String: Converter]
    var origin: Double?
    var levels: [String: LevelReading] = [:]
    var receivedFrames: [String: Int] = [:]
    var maximumRMS: [String: Double] = [:]
    var requestedSources: [String] = []
    var stream: SCStream?
    var microphoneSession: AVCaptureSession?
    let stop: AsyncStream<Void>.Continuation
    init(tracks: [String: AppleTrack], stop: AsyncStream<Void>.Continuation) {
        self.tracks = tracks
        self.stop = stop
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
        self.converters = ["mic": Converter(target: format), "system": Converter(target: format)]
    }
    func accept(_ buffer: AVAudioPCMBuffer, source: String, time: Double) {
        do {
            if origin == nil { origin = time }
            let seconds = max(0, time - origin!)
            let pcm = try converters[source]!.convert(buffer)
            guard pcm.frameLength > 0, let samples = pcm.floatChannelData?[0] else { return }
            receivedFrames[source, default: 0] += Int(pcm.frameLength)
            var reading = levels[source] ?? LevelReading()
            for index in 0..<Int(pcm.frameLength) {
                let sample = samples[index]
                if sample.isFinite {
                    reading.sum += Double(sample) * Double(sample)
                    reading.peak = max(reading.peak, abs(sample))
                    reading.count += 1
                }
            }
            let now = ProcessInfo.processInfo.systemUptime
            if now - reading.emittedAt >= 0.1, reading.count > 0 {
                let rms = sqrt(reading.sum / Double(reading.count))
                maximumRMS[source] = max(maximumRMS[source, default: 0], rms)
                emit(["type": "level", "source": source,
                      "rms": rms, "peak": Double(reading.peak)])
                reading = LevelReading(emittedAt: now)
            }
            levels[source] = reading
            if let track = tracks[source] {
                try track.add(buffer, at: seconds)
            } else {
                let data = Data(bytes: samples, count: Int(pcm.frameLength) * MemoryLayout<Float>.size)
                emit(["type": "audio", "source": source, "start": seconds,
                      "sample_rate": 16000, "pcm": data.base64EncodedString()])
            }
        } catch {
            emit(["type": "error", "message": "Audio conversion: \(error.localizedDescription)"])
            stop.yield(())
        }
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        acceptSample(sampleBuffer, source: "system")
    }
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        acceptSample(sampleBuffer, source: "mic")
    }
    func acceptSample(_ sampleBuffer: CMSampleBuffer, source: String) {
        guard sampleBuffer.isValid,
              let description = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        let frames = CMSampleBufferGetNumSamples(sampleBuffer)
        guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else { return }
        buffer.frameLength = AVAudioFrameCount(frames)
        let result = CMSampleBufferCopyPCMDataIntoAudioBufferList(sampleBuffer, at: 0,
            frameCount: Int32(frames), into: buffer.mutableAudioBufferList)
        guard result == noErr else {
            emit(["type": "error", "message": "Cannot copy capture audio (\(result))"])
            stop.yield(())
            return
        }
        accept(buffer, source: source,
               time: CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer)))
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        emit(["type": "error", "message": "Capture stopped: \(error.localizedDescription)"])
        stop.yield(())
    }
    func startMicrophone(selection: String) throws {
        let device = try AudioDevices.microphone(selection)
        let discovery = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external], mediaType: .audio, position: .unspecified)
        guard let selected = discovery.devices.first(where: { $0.uniqueID == device.uid }) else {
            throw Failure(message: "Microphone \(device.name) is not available to AVFoundation. Run voice-notes devices and select another input.")
        }
        let session = AVCaptureSession()
        let input = try AVCaptureDeviceInput(device: selected)
        let output = AVCaptureAudioDataOutput()
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddInput(input), session.canAddOutput(output) else {
            throw Failure(message: "Cannot configure microphone \(device.name)")
        }
        session.beginConfiguration()
        session.addInput(input)
        session.addOutput(output)
        session.commitConfiguration()
        microphoneSession = session
        session.startRunning()
        guard session.isRunning else { throw Failure(message: "Microphone \(device.name) did not start") }
        let format = AVAudioFormat(cmAudioFormatDescription: selected.activeFormat.formatDescription)
        emit(["type": "status", "message": "Microphone: \(device.name) (\(device.uid)); \(Int(format.sampleRate)) Hz, \(format.channelCount) channels", "device": device.json])
    }
    func start(source: String, bundle: String, mic: String) async throws {
        requestedSources = source == "both" ? ["mic", "system"] : [source]
        if source != "system" {
            guard await AVCaptureDevice.requestAccess(for: .audio) else {
                throw Failure(message: "Microphone permission denied. Enable it for your terminal in System Settings > Privacy & Security.")
            }
        }
        if source == "mic" {
            try startMicrophone(selection: mic)
            return
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw Failure(message: "No display is available for system audio capture") }
        let filter: SCContentFilter
        if bundle == "all" {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        } else {
            let applications = content.applications.filter { $0.bundleIdentifier == bundle }
            guard !applications.isEmpty else {
                throw Failure(message: "No running app with bundle ID \(bundle). Open that app first, or use --app all for all speaker audio.")
            }
            filter = SCContentFilter(display: display, including: applications, exceptingWindows: [])
        }
        let config = SCStreamConfiguration()
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(seconds: 1, preferredTimescale: 1)
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 16000
        config.channelCount = 1
        config.captureMicrophone = false
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        self.stream = stream
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        // Keep a minimal screen sink registered so the stream has a complete
        // output graph; screen samples are discarded immediately by our guard.
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try await stream.startCapture()
        let outputID = AudioDevices.defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
        let output = AudioDevices.string(outputID, kAudioObjectPropertyName)
        emit(["type": "status", "message": "System audio: \(bundle == "all" ? "all applications" : bundle); default output device: \(output)"])
        if source == "both" { try startMicrophone(selection: mic) }
    }
    func finish() async throws {
        microphoneSession?.stopRunning()
        if let stream { try await stream.stopCapture() }
        await withCheckedContinuation { continuation in queue.async { continuation.resume() } }
        var statistics: [String: Any] = [:]
        for source in requestedSources {
            let frames = receivedFrames[source, default: 0]
            statistics[source] = ["frames": frames, "maximum_rms": maximumRMS[source, default: 0]]
            if frames == 0 {
                let advice = source == "mic"
                    ? "Check the selected microphone with voice-notes devices and --mic."
                    : "Check that the selected app is playing audio, or test --app all."
                emit(["type": "warning", "message": "No \(source) audio frames were received. \(advice)"])
            }
        }
        emit(["type": "capture_stats", "sources": statistics])
        for track in tracks.values { try await track.finish() }
    }
}

@main struct Main {
    static func main() async {
        do {
            let args = CommandLine.arguments
            func option(_ name: String, _ fallback: String) -> String {
                guard let index = args.firstIndex(of: name), index + 1 < args.count else { return fallback }
                return args[index + 1]
            }
            let locale = Locale(identifier: option("--locale", "en-US"))
            if args.contains("--devices") {
                emit(["type": "devices", "devices": try AudioDevices.all().map(\.json)])
                return
            }
            if args.contains("--doctor") {
                emit(["type": "doctor", "apple_available": SpeechTranscriber.isAvailable,
                      "installed_locales": await SpeechTranscriber.installedLocales.map(\.identifier),
                      "supported_locales": await SpeechTranscriber.supportedLocales.map(\.identifier),
                      "screen_permission": CGPreflightScreenCaptureAccess(),
                      "microphone_permission": AVCaptureDevice.authorizationStatus(for: .audio).rawValue])
                return
            }
            let source = option("--source", "both")
            let engine = option("--engine", "apple")
            let (stops, continuation) = AsyncStream<Void>.makeStream()
            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)
            let signals = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
                let signal = DispatchSource.makeSignalSource(signal: number, queue: .global())
                signal.setEventHandler { continuation.yield(()) }
                signal.resume()
                return signal
            }
            defer { for signal in signals { signal.cancel() } }
            var tracks: [String: AppleTrack] = [:]
            if engine == "apple" {
                for name in (source == "both" ? ["mic", "system"] : [source]) {
                    tracks[name] = try await AppleTrack.create(source: name, locale: locale)
                }
            }
            let capture = Capture(tracks: tracks, stop: continuation)
            try await capture.start(source: source, bundle: option("--app", "all"), mic: option("--mic", ""))
            emit(["type": "ready", "message": "Recording \(source) audio with \(engine). Press Ctrl-C to stop."])
            for await _ in stops { break }
            try await capture.finish()
            emit(["type": "stopped"])
        } catch {
            let nativeError = error as NSError
            if nativeError.domain == SCStreamErrorDomain,
               nativeError.code == SCStreamError.Code.userDeclined.rawValue {
                emit(["type": "error", "code": "screen_permission_denied",
                      "message": "Screen/system audio permission denied. Open System Settings > Privacy & Security > Screen & System Audio Recording and enable the terminal/app that launched voice-notes (or Voice Notes Capture, if listed). Quit and reopen that terminal/app, then retry. Use --source mic for microphone-only recording."])
            } else {
                emit(["type": "error", "message": error.localizedDescription])
            }
            exit(1)
        }
    }
}
