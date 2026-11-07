import Foundation

enum ConnectionState: Equatable {
    case offline
    case connecting
    case live
    case demo
    case error(String)

    var label: String {
        switch self {
        case .offline: return "offline"
        case .connecting: return "connecting"
        case .live: return "live"
        case .demo: return "demo"
        case .error: return "offline"
        }
    }
}

/// WebSocket client for `WS /ws/events` with auto-reconnect (exponential backoff)
/// and keep-alive pings. All callbacks are delivered on the main actor.
@MainActor
final class EventSocket {
    var onEvent: ((RavenEvent) -> Void)?
    var onState: ((ConnectionState) -> Void)?

    private var task: URLSessionWebSocketTask?
    private var url: URL?
    private var active = false
    private var attempt = 0
    private var reconnectTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private let session = URLSession(configuration: .default)

    func connect(to url: URL) {
        disconnect()
        self.url = url
        active = true
        attempt = 0
        open()
    }

    func disconnect() {
        active = false
        reconnectTask?.cancel()
        reconnectTask = nil
        pingTask?.cancel()
        pingTask = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func open() {
        guard active, let url else { return }
        onState?(.connecting)
        let t = session.webSocketTask(with: url)
        t.maximumMessageSize = 32 * 1024 * 1024
        task = t
        t.resume()
        listen(t)
        pingTask?.cancel()
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                let ok = await EventSocket.ping(t)
                guard let strong = self, strong.task === t, !Task.isCancelled else { return }
                if ok {
                    strong.attempt = 0
                    strong.onState?(.live)
                } else {
                    strong.fail(t, reason: "ping failed")
                    return
                }
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    nonisolated private static func ping(_ t: URLSessionWebSocketTask) async -> Bool {
        await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            t.sendPing { @Sendable error in
                cont.resume(returning: error == nil)
            }
        }
    }

    private func listen(_ t: URLSessionWebSocketTask) {
        t.receive { @Sendable [weak self] result in
            guard let self else { return }
            Task { @MainActor in
                self.handle(result, from: t)
            }
        }
    }

    private func handle(_ result: Result<URLSessionWebSocketTask.Message, Error>, from t: URLSessionWebSocketTask) {
        guard task === t else { return }
        switch result {
        case .success(let message):
            if attempt != 0 { attempt = 0 }
            onState?(.live)
            switch message {
            case .string(let s): decode(Data(s.utf8))
            case .data(let d): decode(d)
            @unknown default: break
            }
            listen(t)
        case .failure(let error):
            fail(t, reason: error.localizedDescription)
        }
    }

    private func decode(_ data: Data) {
        let decoder = JSONDecoder()
        if let e = try? decoder.decode(RavenEvent.self, from: data) {
            onEvent?(e)
        } else if let list = try? decoder.decode([RavenEvent].self, from: data) {
            list.forEach { onEvent?($0) }
        }
    }

    private func fail(_ t: URLSessionWebSocketTask, reason: String) {
        guard task === t else { return }
        t.cancel(with: .abnormalClosure, reason: nil)
        task = nil
        pingTask?.cancel()
        guard active else { return }
        onState?(.error(reason))
        attempt += 1
        let delay = min(pow(2.0, Double(attempt - 1)), 10.0)
        reconnectTask?.cancel()
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.open()
        }
    }
}
