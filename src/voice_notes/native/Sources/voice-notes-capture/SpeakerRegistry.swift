import Foundation
import FluidAudio

/// One enrolled voice. Records are created automatically for unknown speakers;
/// the Python CLI later adds a name (`voice-notes speakers name`).
struct SpeakerRecord {
    var id: String
    var embedding: [Float]
    var embeddingSeconds: Double
    var sampleSeconds: Double
    var createdAt: String
}

/// Persistent speaker registry. One directory per UUID:
///   speaker.json  name, embedding, timestamps (shared with speakers.py)
///   sample.wav    up to `sampleLimit` seconds of that speaker's speech
/// Accessed from one task at a time; tracks share it through `FluidModels`.
final class SpeakerStore {
    static let sampleLimit = 60.0
    static let weightLimit = 120.0
    static let sampleRate = 16000

    let root: URL
    private(set) var records: [String: SpeakerRecord] = [:]
    private var pendingSamples: [String: [Float]] = [:]
    private let lock = NSLock()

    init(root: URL) throws {
        self.root = root
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let entries = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        for entry in entries {
            let id = entry.lastPathComponent.lowercased()
            guard UUID(uuidString: id) != nil,
                  let data = try? Data(contentsOf: entry.appendingPathComponent("speaker.json")),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            if let merged = json["merged_into"] as? String, !merged.isEmpty { continue }
            guard let numbers = json["embedding"] as? [Any] else { continue }
            let vector = numbers.compactMap { ($0 as? NSNumber)?.floatValue }
            guard vector.count == CampPlusEmbedder.embeddingDim else { continue }
            records[id] = SpeakerRecord(
                id: id, embedding: vector,
                embeddingSeconds: (json["embedding_seconds"] as? NSNumber)?.doubleValue ?? 10,
                sampleSeconds: (json["sample_seconds"] as? NSNumber)?.doubleValue ?? 0,
                createdAt: json["created_at"] as? String ?? Self.timestamp())
        }
    }

    static func timestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }

    func match(_ embedding: [Float]) -> (id: String, similarity: Float)? {
        lock.lock(); defer { lock.unlock() }
        var best: (id: String, similarity: Float)?
        for record in records.values {
            let similarity = CampPlusEmbedder.cosine(embedding, record.embedding)
            if best == nil || similarity > best!.similarity { best = (record.id, similarity) }
        }
        return best
    }

    func create(embedding: [Float], seconds: Double) throws -> String {
        lock.lock(); defer { lock.unlock() }
        let id = UUID().uuidString.lowercased()
        records[id] = SpeakerRecord(id: id, embedding: embedding,
                                    embeddingSeconds: min(seconds, Self.weightLimit),
                                    sampleSeconds: 0, createdAt: Self.timestamp())
        try save(id)
        return id
    }

    /// Fold a confident new observation into the speaker's centroid.
    func update(_ id: String, embedding: [Float], seconds: Double) throws {
        lock.lock(); defer { lock.unlock() }
        guard var record = records[id] else { return }
        let old = Float(record.embeddingSeconds), new = Float(seconds)
        var merged = zip(record.embedding, embedding).map { ($0 * old + $1 * new) / (old + new) }
        let norm = max(sqrt(merged.reduce(0) { $0 + $1 * $1 }), 1e-9)
        merged = merged.map { $0 / norm }
        record.embedding = merged
        record.embeddingSeconds = min(record.embeddingSeconds + seconds, Self.weightLimit)
        records[id] = record
        try save(id)
    }

    /// Append speech to the speaker's audio sample until it reaches the limit.
    func appendSample(_ id: String, audio: [Float]) throws {
        lock.lock(); defer { lock.unlock() }
        guard var record = records[id], record.sampleSeconds < Self.sampleLimit, !audio.isEmpty else { return }
        var samples = pendingSamples[id] ?? loadSample(id)
        let room = Int(Self.sampleLimit) * Self.sampleRate - samples.count
        guard room > 0 else { return }
        samples.append(contentsOf: audio.prefix(room))
        record.sampleSeconds = Double(samples.count) / Double(Self.sampleRate)
        records[id] = record
        try writeWav(samples, to: directory(id).appendingPathComponent("sample.wav"))
        // Keep the buffer only while the sample can still grow.
        pendingSamples[id] = record.sampleSeconds < Self.sampleLimit ? samples : nil
        try save(id)
    }

    private func directory(_ id: String) -> URL { root.appendingPathComponent(id) }

    private func loadSample(_ id: String) -> [Float] {
        guard let data = try? Data(contentsOf: directory(id).appendingPathComponent("sample.wav")),
              data.count > 44 else { return [] }
        let body = data.dropFirst(44)
        return stride(from: body.startIndex, to: body.endIndex - 1, by: 2).map { index in
            Float(Int16(bitPattern: UInt16(body[index]) | UInt16(body[index + 1]) << 8)) / 32768
        }
    }

    private func writeWav(_ samples: [Float], to url: URL) throws {
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        let bytes = UInt32(samples.count * 2)
        data.append(contentsOf: Array("RIFF".utf8)); append(36 + bytes)
        data.append(contentsOf: Array("WAVEfmt ".utf8)); append(UInt32(16))
        append(UInt16(1)); append(UInt16(1)); append(UInt32(Self.sampleRate))
        append(UInt32(Self.sampleRate * 2)); append(UInt16(2)); append(UInt16(16))
        data.append(contentsOf: Array("data".utf8)); append(bytes)
        for sample in samples { append(Int16(max(-1, min(1, sample)) * 32767)) }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    }

    /// Merge our fields into any existing speaker.json so a name added by the
    /// CLI is never overwritten.
    private func save(_ id: String) throws {
        guard let record = records[id] else { return }
        let file = directory(id).appendingPathComponent("speaker.json")
        try FileManager.default.createDirectory(at: directory(id), withIntermediateDirectories: true)
        var json = ((try? Data(contentsOf: file)).flatMap {
            try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
        }) ?? [:]
        json["id"] = id
        json["created_at"] = json["created_at"] ?? record.createdAt
        json["name"] = json["name"] ?? NSNull()
        json["embedding"] = record.embedding.map { Double($0) }
        json["embedding_seconds"] = record.embeddingSeconds
        json["sample_seconds"] = record.sampleSeconds
        json["updated_at"] = Self.timestamp()
        let data = try JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: file, options: .atomic)
    }
}
