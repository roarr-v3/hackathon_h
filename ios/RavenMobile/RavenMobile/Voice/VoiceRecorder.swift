import AVFoundation
import Foundation
import Observation

enum VoiceError: LocalizedError {
    case permissionDenied, badURL, noInput, converter

    var errorDescription: String? {
        switch self {
        case .permissionDenied: return "Microphone permission denied"
        case .badURL: return "Invalid voice session URL"
        case .noInput: return "No microphone input available"
        case .converter: return "Audio converter unavailable"
        }
    }
}

/// Reference box so audio callbacks can mutate state without capturing a `var`.
private final class Flag: @unchecked Sendable {
    var value = false
}

/// Gladia live STT client.
///
/// Flow: raven-core `POST /voice/session` → `{url, sample_rate:16000, encoding:"wav/pcm", bit_depth:16, channels:1}`.
/// We open a WebSocket to `url`, stream raw 16 kHz mono s16le PCM as binary frames and parse
/// Gladia v2 messages: `{"type":"transcript","data":{"is_final":Bool,"utterance":{"text":String}}}`.
/// To finish we send `{"type":"stop_recording"}`; Gladia flushes remaining finals and closes.
@MainActor
@Observable
final class VoiceRecorder {
    enum Phase: Equatable { case idle, connecting, recording, finishing }

    var phase: Phase = .idle
    var committed = ""
    var partial = ""
    var level: Double = 0
    var errorMessage: String?
    /// When true (tap-to-talk), the first final utterance auto-submits.
    var autoSubmitOnFinal = false

    var liveText: String {
        [committed, partial].filter { !$0.isEmpty }.joined(separator: " ")
    }

    @ObservationIgnored var onFinal: ((String) -> Void)?
    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var socket: URLSessionWebSocketTask?
    @ObservationIgnored private var demoTask: Task<Void, Never>?
    @ObservationIgnored private var gladiaDone = false
    @ObservationIgnored private var errorTask: Task<Void, Never>?

    func beginConnecting() {
        committed = ""
        partial = ""
        level = 0
        errorMessage = nil
        gladiaDone = false
        phase = .connecting
    }

    func fail(_ message: String) {
        cleanup()
        errorMessage = message
        errorTask?.cancel()
        errorTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_500_000_000)
            guard !Task.isCancelled else { return }
            self?.errorMessage = nil
        }
    }

    // MARK: Live (Gladia)

    func start(session: VoiceSession) async throws {
        guard phase == .connecting else { return }
        let granted = await AVAudioApplication.requestRecordPermission()
        guard granted else { throw VoiceError.permissionDenied }
        guard phase == .connecting else { return } // cancelled while asking
        guard let url = URL(string: session.url) else { throw VoiceError.badURL }

        let av = AVAudioSession.sharedInstance()
        try av.setCategory(.record, mode: .measurement, options: [.duckOthers])
        try av.setActive(true, options: .notifyOthersOnDeactivation)

        let task = URLSession.shared.webSocketTask(with: url)
        socket = task
        task.resume()
        listen(task)

        try startEngine(sampleRate: Double(session.sampleRate > 0 ? session.sampleRate : 16000), socket: task)
        phase = .recording
    }

    private func startEngine(sampleRate: Double, socket: URLSessionWebSocketTask) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0 else { throw VoiceError.noInput }
        guard let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: inFormat, to: outFormat) else { throw VoiceError.converter }

        let levelSink: @Sendable (Double) -> Void = { [weak self] value in
            guard let self else { return }
            Task { @MainActor in
                if self.phase == .recording { self.level = value }
            }
        }
        let tap = VoiceRecorder.makeTap(converter: converter, inFormat: inFormat, outFormat: outFormat,
                                        socket: socket, onLevel: levelSink)
        input.installTap(onBus: 0, bufferSize: 2048, format: inFormat, block: tap)
        engine.prepare()
        try engine.start()
        self.engine = engine
    }

    /// Built outside the main actor so the realtime audio callback never inherits actor isolation.
    nonisolated private static func makeTap(converter: AVAudioConverter, inFormat: AVAudioFormat, outFormat: AVAudioFormat,
                                            socket: URLSessionWebSocketTask,
                                            onLevel: @escaping @Sendable (Double) -> Void) -> AVAudioNodeTapBlock {
        let counter = Counter()
        return { buffer, _ in
            let ratio = outFormat.sampleRate / inFormat.sampleRate
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
            guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
            let fed = Flag()
            var error: NSError?
            let status = converter.convert(to: out, error: &error) { _, inputStatus in
                if fed.value {
                    inputStatus.pointee = .noDataNow
                    return nil
                }
                fed.value = true
                inputStatus.pointee = .haveData
                return buffer
            }
            guard status != .error, out.frameLength > 0, let channels = out.int16ChannelData else { return }
            let count = Int(out.frameLength)
            let samples = channels[0]
            let data = Data(bytes: samples, count: count * MemoryLayout<Int16>.size)
            socket.send(.data(data)) { _ in }

            counter.value += 1
            if counter.value % 3 == 0 {
                var sum: Double = 0
                for i in 0..<count {
                    let v = Double(samples[i]) / 32768.0
                    sum += v * v
                }
                let rms = (sum / Double(max(count, 1))).squareRoot()
                onLevel(min(1, rms * 8))
            }
        }
    }

    private func listen(_ task: URLSessionWebSocketTask) {
        task.receive { @Sendable [weak self] result in
            guard let self else { return }
            var text: String?
            var failed = false
            switch result {
            case .success(let message):
                switch message {
                case .string(let s): text = s
                case .data(let d): text = String(decoding: d, as: UTF8.self)
                @unknown default: break
                }
            case .failure:
                failed = true
            }
            let payload = text
            let didFail = failed
            Task { @MainActor in
                guard self.socket === task else { return }
                if didFail {
                    self.gladiaDone = true
                    if self.phase == .recording {
                        self.fail("Voice connection closed")
                    }
                    return
                }
                if let payload { self.handleGladia(payload) }
                self.listen(task)
            }
        }
    }

    private func handleGladia(_ text: String) {
        guard let data = text.data(using: .utf8),
              let msg = try? JSONDecoder().decode(JSONValue.self, from: data) else { return }
        let type = msg["type"]?.string ?? ""
        switch type {
        case "transcript":
            guard let d = msg["data"] else { return }
            let utterance = (d["utterance"]?["text"]?.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if d["is_final"]?.bool == true {
                if !utterance.isEmpty {
                    committed = committed.isEmpty ? utterance : committed + " " + utterance
                }
                partial = ""
                if autoSubmitOnFinal, phase == .recording, !utterance.isEmpty {
                    onFinal?(liveText)
                }
            } else {
                partial = utterance
            }
        case "post_final_transcript", "final_transcript", "end_session", "end_recording":
            gladiaDone = true
        case "error":
            errorMessage = msg["error"]?["message"]?.string ?? msg["message"]?.string ?? "Transcription error"
        default:
            break
        }
    }

    /// Stops capture, flushes Gladia, and returns the final transcript.
    func stop() async -> String {
        switch phase {
        case .idle, .finishing:
            return ""
        case .connecting:
            cancel()
            return ""
        case .recording:
            break
        }
        phase = .finishing
        stopEngine()

        if let demoTask {
            demoTask.cancel()
            self.demoTask = nil
        } else if let socket {
            socket.send(.string("{\"type\":\"stop_recording\"}")) { _ in }
            let deadline = Date().addingTimeInterval(2.5)
            while !gladiaDone && Date() < deadline {
                try? await Task.sleep(nanoseconds: 60_000_000)
            }
        }
        if !partial.isEmpty {
            committed = committed.isEmpty ? partial : committed + " " + partial
            partial = ""
        }
        let result = committed.trimmingCharacters(in: .whitespacesAndNewlines)
        cleanup()
        return result
    }

    func cancel() {
        cleanup()
        committed = ""
        partial = ""
    }

    private func stopEngine() {
        if let engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        engine = nil
        level = 0
    }

    private func cleanup() {
        stopEngine()
        demoTask?.cancel()
        demoTask = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        phase = .idle
        autoSubmitOnFinal = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    // MARK: Demo

    /// Simulated live transcription: words stream in as partials, then a final.
    func startDemo(phrase: String) {
        phase = .recording
        let words = phrase.split(separator: " ").map(String.init)
        demoTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 450_000_000)
            var current: [String] = []
            for w in words {
                guard !Task.isCancelled else { return }
                current.append(w)
                self?.partial = current.joined(separator: " ")
                self?.level = Double.random(in: 0.25...0.95)
                try? await Task.sleep(nanoseconds: UInt64.random(in: 180_000_000...320_000_000))
            }
            guard !Task.isCancelled, let self else { return }
            self.committed = phrase
            self.partial = ""
            self.level = 0.05
            if self.autoSubmitOnFinal { self.onFinal?(self.liveText) }
        }
    }
}

private final class Counter: @unchecked Sendable {
    var value = 0
}
