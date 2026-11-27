import Foundation
import AVFoundation
import FluidAudio

/// Models shared by every track: the voice embedder and the speaker registry.
final class FluidModels {
    let embedder: CampPlusEmbedder
    let store: SpeakerStore
    /// Cosine similarity at or above which a voice is the same speaker.
    let threshold: Float
    /// Base folder holding `campplus/`, `parakeet-unified-en-0.6b/` and `sortformer/`
    /// (for example the repository's `models/fluid`); nil uses FluidAudio's cache.
    let modelsDirectory: URL?

    init(embedder: CampPlusEmbedder, store: SpeakerStore, threshold: Float, modelsDirectory: URL?) {
        self.embedder = embedder
        self.store = store
        self.threshold = threshold
        self.modelsDirectory = modelsDirectory
    }

    static func load(speakersDirectory: URL, threshold: Float, modelsDirectory: URL?) async throws -> FluidModels {
        let store = try SpeakerStore(root: speakersDirectory)
        let embedder: CampPlusEmbedder
        // CAM++ has no directory option for download, so load a local copy directly when present.
        if let local = modelsDirectory?.appendingPathComponent("campplus", isDirectory: true),
           CampPlusModels.modelsExist(at: local) {
            embedder = CampPlusEmbedder(models: try CampPlusModels.load(from: local))
        } else {
            embedder = try await CampPlusEmbedder.load()
        }
        return FluidModels(embedder: embedder, store: store, threshold: threshold, modelsDirectory: modelsDirectory)
    }
}

/// One audio track (microphone or system audio): streaming speech recognition,
/// streaming diarization, and speaker identification against the registry.
///
/// `add` is called from the capture queue. Everything else runs on one task, so
/// the mutable state below needs no locking.
final class FluidTrack {
    static let rate = 16000
    /// Speech shorter than this inherits the speaker last seen in the same slot.
    static let minEmbeddingSeconds = 1.2
    /// A new unknown speaker needs at least this much speech in one run.
    static let minEnrollmentSeconds = 2.0
    /// Embeddings from at least this long a run may refine a speaker's centroid.
    static let updateSimilarity: Float = 0.75
    /// Similarities this close below the threshold are ambiguous, not new voices.
    static let ambiguityMargin: Float = 0.10
    static let historySeconds = 120
    /// A trailing utterance is closed once this much audio follows its last word.
    static let silenceToClose = 5.0
    static let maxWordSeconds = 0.6

    let source: String
    private let shared: FluidModels
    private let asr: StreamingUnifiedAsrManager
    private let diarizer: SortformerDiarizer
    private let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
    private let stream: AsyncStream<[Float]>
    private let continuation: AsyncStream<[Float]>.Continuation
    private var worker: Task<Void, Never>?

    // Producer side: touched only by the capture queue.
    private var produced = 0

    // Consumer side: touched only by the worker task.
    private var history: [Float] = []
    private var historyStart = 0
    private var fed = 0
    // Tokens arrive in batches that can end mid-word, so words are built only
    // once the next word has started (or the stream has gone quiet).
    private var tokenBuffer: [TokenTiming] = []
    private var pending: [WordTiming] = []
    private var lastPartial = ""
    private var slotMemory: [Int: String] = [:]
    private var counter = 0
    private var failed = false

    private init(source: String, shared: FluidModels, asr: StreamingUnifiedAsrManager, diarizer: SortformerDiarizer) {
        self.source = source
        self.shared = shared
        self.asr = asr
        self.diarizer = diarizer
        (stream, continuation) = AsyncStream<[Float]>.makeStream()
    }

    static func create(source: String, shared: FluidModels) async throws -> FluidTrack {
        let asr = StreamingUnifiedAsrManager()
        // Both loaders use files already in the directory and download only what is missing.
        try await asr.loadModels(to: shared.modelsDirectory)
        let config = SortformerConfig.default
        let diarizer = SortformerDiarizer(config: config)
        diarizer.initialize(models: try await SortformerModels.loadFromHuggingFace(
            config: config, cacheDirectory: shared.modelsDirectory))
        let track = FluidTrack(source: source, shared: shared, asr: asr, diarizer: diarizer)
        track.worker = Task { await track.run() }
        return track
    }

    // MARK: Producer

    /// Queue 16 kHz mono samples that begin at `seconds` on the shared timeline.
    func add(_ samples: [Float], at seconds: Double) {
        let target = Int((seconds * Double(Self.rate)).rounded())
        var gap = target - produced
        // A gap over 20 ms means the source was silent or dropped frames; keep
        // the track aligned with the meeting clock so timestamps stay true.
        if gap > Self.rate / 50 {
            while gap > 0 {
                let count = min(gap, Self.rate)
                continuation.yield([Float](repeating: 0, count: count))
                produced += count
                gap -= count
            }
        }
        var body = samples
        let overlap = produced - target
        if overlap > Self.rate / 50 { body = Array(samples.dropFirst(min(overlap, samples.count))) }
        guard !body.isEmpty else { return }
        produced += body.count
        continuation.yield(body)
    }

    /// Stop accepting audio and wait until every queued sample is transcribed.
    func finish() async {
        continuation.finish()
        await worker?.value
    }

    // MARK: Consumer

    private func run() async {
        for await chunk in stream where !failed {
            do { try await process(chunk) } catch { fail(error) }
        }
        guard !failed else { return }
        do { try await flush() } catch { fail(error) }
    }

    private func fail(_ error: Error) {
        failed = true
        emit(["type": "error", "message": "\(source) recognition: \(error.localizedDescription)"])
    }

    private func process(_ chunk: [Float]) async throws {
        history.append(contentsOf: chunk)
        fed += chunk.count
        let limit = Self.historySeconds * Self.rate
        if history.count > limit + Self.rate * 10 {
            let drop = history.count - limit
            history.removeFirst(drop)
            historyStart += drop
        }
        diarizer.addAudio(chunk)
        try diarizer.process()
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(chunk.count))!
        buffer.frameLength = AVAudioFrameCount(chunk.count)
        chunk.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: chunk.count) }
        try await asr.appendAudio(buffer)
        try await asr.processBufferedAudio()
        tokenBuffer.append(contentsOf: await asr.consumeTokenTimings())
        ingestCompleteWords(final: false)
        try await emitUtterances(final: false)
        emitPartial()
    }

    private func flush() async throws {
        _ = try await asr.finish()
        tokenBuffer.append(contentsOf: await asr.consumeTokenTimings())
        ingestCompleteWords(final: true)
        _ = try diarizer.finalizeSession()
        try await emitUtterances(final: true)
    }

    /// RNNT tokens have no duration; the decoder stretches each word to the next
    /// word's start, so a sentence's last word absorbs the pause that follows it.
    /// Cap it, or silence is attributed to (and embedded as) speech.
    private func spokenEnd(_ word: WordTiming) -> Double {
        min(word.endTime, word.startTime + Self.maxWordSeconds)
    }

    private func startsWord(_ token: TokenTiming) -> Bool { token.token.hasPrefix(" ") }

    /// Turn buffered sub-word tokens into words, keeping a trailing word that
    /// may still be growing (or waiting for its punctuation).
    private func ingestCompleteWords(final: Bool) {
        guard !tokenBuffer.isEmpty else { return }
        var cut = tokenBuffer.count
        let quiet = Double(fed) / Double(Self.rate) - tokenBuffer[tokenBuffer.count - 1].startTime >= 3.0
        if !final, !quiet {
            guard let lastStart = tokenBuffer.lastIndex(where: { startsWord($0) }) else { return }
            cut = lastStart
        }
        guard cut > 0 else { return }
        let tokens = Array(tokenBuffer.prefix(cut))
        tokenBuffer.removeFirst(cut)
        var words = buildWordTimings(from: tokens)
        // A leading token without a space continues the previous word, such as
        // punctuation that arrived after a quiet spell. Text already emitted is final.
        if !startsWord(tokens[0]), let first = words.first {
            words.removeFirst()
            if let last = pending.last {
                pending[pending.count - 1] = WordTiming(word: last.word + first.word, startTime: last.startTime,
                                                       endTime: max(last.endTime, first.endTime))
            }
        }
        pending.append(contentsOf: words)
    }

    private func text(_ words: ArraySlice<WordTiming>) -> String {
        words.map { $0.word.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }.joined(separator: " ")
    }

    private func emitPartial() {
        guard let first = pending.first, let last = pending.last else { return }
        let current = text(pending[...])
        guard current != lastPartial, !current.isEmpty else { return }
        lastPartial = current
        emit(["type": "partial", "source": source, "id": "u\(counter)",
              "start": first.startTime, "end": max(spokenEnd(last), first.startTime), "text": current])
    }

    private func endsSentence(_ word: String) -> Bool {
        let stripped = word.trimmingCharacters(in: CharacterSet(charactersIn: "\"')”’ "))
        return stripped.hasSuffix(".") || stripped.hasSuffix("?") || stripped.hasSuffix("!")
    }

    /// Number of leading words that form a finished utterance, if any.
    private func utteranceLength(final: Bool) -> Int? {
        guard !pending.isEmpty else { return nil }
        if final { return pending.count }
        let now = Double(fed) / Double(Self.rate)
        for index in pending.indices {
            let word = pending[index]
            if index + 1 < pending.count {
                let gap = pending[index + 1].startTime - spokenEnd(word)
                if gap >= 1.5 || (endsSentence(word.word) && gap >= 0.7) { return index + 1 }
                if endsSentence(word.word), spokenEnd(word) - pending[0].startTime >= 25 { return index + 1 }
            } else if now - spokenEnd(word) >= Self.silenceToClose {
                return index + 1
            }
        }
        return nil
    }

    private func emitUtterances(final: Bool) async throws {
        let diarized = Double(diarizer.numFramesProcessed) * Double(diarizer.config.frameDurationSeconds)
        while let length = utteranceLength(final: final) {
            let words = Array(pending.prefix(length))
            // Speaker attribution needs diarization to have caught up with the words.
            if !final, diarized < spokenEnd(words[words.count - 1]) + 0.2 { break }
            pending.removeFirst(length)
            lastPartial = ""
            try await emitSegments(words)
        }
    }

    // MARK: Speaker attribution

    private struct Span { let slot: Int; let start: Double; let end: Double }

    /// Diarizer spans that can touch words between `start` and `end`. The timeline holds every
    /// span since the helper started, so filtering keeps attribution cost flat through an all-day run.
    private func spans(from start: Double, to end: Double) -> [Span] {
        // Words in a gap fall back to the nearest span within 0.6 s (see `slot(of:in:)`).
        let lower = start - 1.0, upper = end + 1.0
        var result: [Span] = []
        for (slot, speaker) in diarizer.timeline.speakers {
            for segments in [speaker.finalizedSegments, speaker.tentativeSegments] {
                for segment in segments {
                    let spanStart = Double(segment.startTime), spanEnd = Double(segment.endTime)
                    if spanEnd >= lower && spanStart <= upper {
                        result.append(Span(slot: slot, start: spanStart, end: spanEnd))
                    }
                }
            }
        }
        return result
    }

    private func slot(of word: WordTiming, in spans: [Span]) -> Int? {
        var overlap: [Int: Double] = [:]
        for span in spans {
            let shared = min(span.end, spokenEnd(word)) - max(span.start, word.startTime)
            if shared > 0 { overlap[span.slot, default: 0] += shared }
        }
        if let best = overlap.max(by: { $0.value < $1.value }) { return best.key }
        // A word in a diarization gap belongs to the nearest speaker within 0.6 s.
        let middle = (word.startTime + spokenEnd(word)) / 2
        let near = spans.map { ($0.slot, max($0.start - middle, middle - $0.end, 0)) }.min { $0.1 < $1.1 }
        if let near, near.1 <= 0.6 { return near.0 }
        return nil
    }

    private struct Run { var slot: Int?; var words: [WordTiming] }

    private func runs(_ words: [WordTiming]) -> [Run] {
        guard let first = words.first, let last = words.last else { return [] }
        let spans = spans(from: first.startTime, to: spokenEnd(last))
        var result: [Run] = []
        for word in words {
            let slot = self.slot(of: word, in: spans) ?? result.last?.slot
            if var last = result.last, slot == nil || slot == last.slot {
                last.words.append(word)
                result[result.count - 1] = last
            } else {
                result.append(Run(slot: slot, words: [word]))
            }
        }
        // A brief interjection is more likely a diarization wobble than a turn.
        func brief(_ run: Run) -> Bool {
            run.words.count < 3 && (spokenEnd(run.words.last!) - run.words.first!.startTime) < 0.8
        }
        var index = 1
        while index < result.count {
            if brief(result[index]) {
                result[index - 1].words.append(contentsOf: result[index].words)
                result.remove(at: index)
            } else { index += 1 }
        }
        if result.count > 1, brief(result[0]) {
            result[1].words.insert(contentsOf: result[0].words, at: 0)
            result.removeFirst()
        }
        return result
    }

    private func audio(from start: Double, to end: Double) -> [Float] {
        let lower = max(Int(start * Double(Self.rate)) - historyStart, 0)
        let upper = min(Int(end * Double(Self.rate)) - historyStart, history.count)
        return upper > lower ? Array(history[lower..<upper]) : []
    }

    private func emitSegments(_ words: [WordTiming]) async throws {
        for run in runs(words) {
            let start = run.words.first!.startTime
            let end = max(spokenEnd(run.words.last!), start)
            let speaker = await identify(run, start: start, end: end)
            counter += 1
            var event: [String: Any] = ["type": "segment", "source": source, "id": "u\(counter)",
                                        "start": start, "end": end, "text": text(run.words[...])]
            if let speaker { event["speaker_uuid"] = speaker }
            emit(event)
        }
    }

    /// Match a run's voice to the registry, enrolling an unknown speaker when
    /// there is enough speech and no plausible match.
    private func identify(_ run: Run, start: Double, end: Double) async -> String? {
        let seconds = end - start
        let samples = audio(from: start, to: end)
        var chosen: String?
        if seconds >= Self.minEmbeddingSeconds, samples.count >= Self.rate {
            do {
                let embedding = try await shared.embedder.embed(audio: samples)
                let best = shared.store.match(embedding)
                if let best, best.similarity >= shared.threshold {
                    chosen = best.id
                    if best.similarity >= Self.updateSimilarity, seconds >= Self.minEnrollmentSeconds {
                        try shared.store.update(best.id, embedding: embedding, seconds: seconds)
                    }
                } else if seconds >= Self.minEnrollmentSeconds,
                          best == nil || best!.similarity < shared.threshold - Self.ambiguityMargin
                              || (run.slot.flatMap { slotMemory[$0] } == nil) {
                    let id = try shared.store.create(embedding: embedding, seconds: seconds)
                    chosen = id
                    emit(["type": "speaker", "source": source, "speaker_uuid": id, "created": true,
                          "message": "New unknown speaker \(id.prefix(8)) on the \(source) track. "
                                     + "Name them later with: voice-notes speakers name \(id.prefix(8)) \"Name\""])
                }
            } catch {
                emit(["type": "warning", "message": "\(source) speaker identification: \(error.localizedDescription)"])
            }
        }
        if chosen == nil, let slot = run.slot { chosen = slotMemory[slot] }
        if let slot = run.slot, let chosen { slotMemory[slot] = chosen }
        if let chosen, seconds >= 1.0 {
            do { try shared.store.appendSample(chosen, audio: samples) } catch {
                emit(["type": "warning", "message": "Could not save speaker sample: \(error.localizedDescription)"])
            }
        }
        return chosen
    }
}
