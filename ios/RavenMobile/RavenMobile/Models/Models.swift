import Foundation

// Mirrors raven-core/src/raven_core/events.py (the wire contract).
// Decoding is deliberately tolerant: unknown enum values and missing optional
// fields never break the whole payload.

enum Platform: String, Codable, CaseIterable, Identifiable, Hashable, Sendable {
    case macos, linux, windows, ios

    var id: String { rawValue }

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Platform(rawValue: raw.lowercased()) ?? .linux
    }

    var displayName: String {
        switch self {
        case .macos: return "macOS"
        case .linux: return "Linux"
        case .windows: return "Windows"
        case .ios: return "iOS"
        }
    }

    var symbol: String {
        switch self {
        case .macos: return "apple.logo"
        case .linux: return "terminal"
        case .windows: return "desktopcomputer"
        case .ios: return "iphone"
        }
    }
}

enum AgentStatus: String, Codable, CaseIterable, Hashable, Sendable {
    case queued, running, passed, failed, stopped, error

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = AgentStatus(rawValue: raw.lowercased()) ?? .queued
    }

    init(raw: String) { self = AgentStatus(rawValue: raw.lowercased()) ?? .queued }

    var isActive: Bool { self == .running || self == .queued }
    var label: String { rawValue }
}

enum RunStatus: String, Codable, CaseIterable, Hashable, Sendable {
    case queued, running, passed, failed, stopped

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = RunStatus(rawValue: raw.lowercased()) ?? .queued
    }

    init(raw: String) { self = RunStatus(rawValue: raw.lowercased()) ?? .queued }

    var asAgentStatus: AgentStatus { AgentStatus(raw: rawValue) }
}

struct Agent: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var runId: String?
    var platform: Platform
    var worker: String
    var title: String
    var task: String
    var status: AgentStatus
    var createdAt: Double
    var lastScreenshot: String?
    var verdictReason: String?

    enum CodingKeys: String, CodingKey {
        case id
        case runId = "run_id"
        case platform, worker, title, task, status
        case createdAt = "created_at"
        case lastScreenshot = "last_screenshot"
        case verdictReason = "verdict_reason"
    }

    init(id: String, runId: String?, platform: Platform, worker: String, title: String, task: String,
         status: AgentStatus, createdAt: Double, lastScreenshot: String? = nil, verdictReason: String? = nil) {
        self.id = id
        self.runId = runId
        self.platform = platform
        self.worker = worker
        self.title = title
        self.task = task
        self.status = status
        self.createdAt = createdAt
        self.lastScreenshot = lastScreenshot
        self.verdictReason = verdictReason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        runId = try c.decodeIfPresent(String.self, forKey: .runId)
        platform = (try? c.decode(Platform.self, forKey: .platform)) ?? .linux
        worker = (try? c.decodeIfPresent(String.self, forKey: .worker)) ?? ""
        task = (try? c.decodeIfPresent(String.self, forKey: .task)) ?? ""
        title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? task
        status = (try? c.decode(AgentStatus.self, forKey: .status)) ?? .queued
        createdAt = (try? c.decodeIfPresent(Double.self, forKey: .createdAt)) ?? Date().timeIntervalSince1970
        lastScreenshot = try? c.decodeIfPresent(String.self, forKey: .lastScreenshot)
        verdictReason = try? c.decodeIfPresent(String.self, forKey: .verdictReason)
    }
}

struct Run: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var spec: String
    var platforms: [Platform]
    var status: RunStatus
    var agentIds: [String]
    var createdAt: Double

    enum CodingKeys: String, CodingKey {
        case id, spec, platforms, status
        case agentIds = "agent_ids"
        case createdAt = "created_at"
    }

    init(id: String, spec: String, platforms: [Platform], status: RunStatus, agentIds: [String], createdAt: Double) {
        self.id = id
        self.spec = spec
        self.platforms = platforms
        self.status = status
        self.agentIds = agentIds
        self.createdAt = createdAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        spec = (try? c.decodeIfPresent(String.self, forKey: .spec)) ?? "spec"
        platforms = (try? c.decodeIfPresent([Platform].self, forKey: .platforms)) ?? []
        status = (try? c.decode(RunStatus.self, forKey: .status)) ?? .queued
        agentIds = (try? c.decodeIfPresent([String].self, forKey: .agentIds)) ?? []
        createdAt = (try? c.decodeIfPresent(Double.self, forKey: .createdAt)) ?? Date().timeIntervalSince1970
    }
}

/// Mirrors `Event` in events.py. Named RavenEvent to avoid clashing with framework types.
struct RavenEvent: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var type: String
    var ts: Double
    var runId: String?
    var agentId: String?
    var data: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case id, type, ts
        case runId = "run_id"
        case agentId = "agent_id"
        case data
    }

    init(id: String = "evt_" + UUID().uuidString.prefix(12).lowercased(), type: String,
         ts: Double = Date().timeIntervalSince1970, runId: String? = nil, agentId: String? = nil,
         data: [String: JSONValue] = [:]) {
        self.id = id
        self.type = type
        self.ts = ts
        self.runId = runId
        self.agentId = agentId
        self.data = data
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        id = (try? c.decodeIfPresent(String.self, forKey: .id)) ?? "evt_" + UUID().uuidString
        ts = (try? c.decodeIfPresent(Double.self, forKey: .ts)) ?? Date().timeIntervalSince1970
        runId = try? c.decodeIfPresent(String.self, forKey: .runId)
        agentId = try? c.decodeIfPresent(String.self, forKey: .agentId)
        data = (try? c.decodeIfPresent([String: JSONValue].self, forKey: .data)) ?? [:]
    }
}

enum EventType {
    static let runCreated = "run.created"
    static let runStatus = "run.status"
    static let agentCreated = "agent.created"
    static let agentStatus = "agent.status"
    static let agentMessage = "agent.message"
    static let agentAction = "agent.action"
    static let agentScreenshot = "agent.screenshot"
    static let agentVerdict = "agent.verdict"
    static let reportReady = "report.ready"
}

extension RavenEvent {
    var date: Date { Date(timeIntervalSince1970: ts) }
    var text: String? { data["text"]?.string }
    var role: String { data["role"]?.string ?? "agent" }
    var actionName: String { data["action"]?.string ?? "action" }
    var actionArgs: [String: JSONValue] { data["args"]?.object ?? [:] }
    var actionLine: String { ActionFormatter.format(action: actionName, args: actionArgs) }
    var thought: String? {
        guard let t = data["thought"]?.string, !t.isEmpty else { return nil }
        return t
    }
    var screenshotURL: String? { data["url"]?.string }
    var imageWidth: Double? { data["width"]?.double }
    var imageHeight: Double? { data["height"]?.double }
    var passed: Bool? { data["passed"]?.bool }
    var reason: String? { data["reason"]?.string }
    var statusValue: String? { data["status"]?.string }
    var agent: Agent? { data["agent"]?.decode(Agent.self) }
    var run: Run? { data["run"]?.decode(Run.self) }
    var markdown: String? { data["markdown"]?.string }
}

enum ActionFormatter {
    /// Renders an action as a compact monospace line: `click 412,88`, `type "hello"`, `key cmd+n`.
    static func format(action: String, args: [String: JSONValue]) -> String {
        var parts = [action]
        var used = Set<String>()
        if let x = args["x"], let y = args["y"] {
            parts.append("\(x.compact),\(y.compact)")
            used.formUnion(["x", "y"])
        }
        if let t = args["text"]?.string {
            let clipped = t.count > 60 ? String(t.prefix(60)) + "…" : t
            parts.append("\"\(clipped)\"")
            used.insert("text")
        }
        for k in ["combo", "key", "keys"] {
            if let v = args[k] {
                parts.append(v.compact)
                used.insert(k)
            }
        }
        if args["dx"] != nil || args["dy"] != nil {
            parts.append("Δ\(args["dx"]?.compact ?? "0"),\(args["dy"]?.compact ?? "0")")
            used.formUnion(["dx", "dy"])
        }
        for k in ["cmd", "command"] {
            if let v = args[k]?.string {
                parts.append("$ " + v)
                used.insert(k)
            }
        }
        if let b = args["button"]?.string, b != "left" {
            parts.append("[\(b)]")
        }
        used.insert("button")
        for k in args.keys.sorted() where !used.contains(k) {
            parts.append("\(k)=\(args[k]!.compact)")
        }
        return parts.joined(separator: " ")
    }
}

struct SpecInfo: Codable, Identifiable, Hashable, Sendable {
    var name: String
    var description: String
    var platforms: [Platform]
    var id: String { name }

    enum CodingKeys: String, CodingKey { case name, description, platforms }

    init(name: String, description: String, platforms: [Platform]) {
        self.name = name
        self.description = description
        self.platforms = platforms
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        description = (try? c.decodeIfPresent(String.self, forKey: .description)) ?? ""
        platforms = (try? c.decodeIfPresent([Platform].self, forKey: .platforms)) ?? Platform.allCases
    }
}

struct AgentDetail: Decodable, Sendable {
    var agent: Agent
    var events: [RavenEvent]
}

struct VoiceSession: Codable, Sendable {
    var id: String
    var url: String
    var sampleRate: Int
    var encoding: String
    var bitDepth: Int
    var channels: Int

    enum CodingKeys: String, CodingKey {
        case id, url, encoding, channels
        case sampleRate = "sample_rate"
        case bitDepth = "bit_depth"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decodeIfPresent(String.self, forKey: .id)) ?? ""
        url = try c.decode(String.self, forKey: .url)
        sampleRate = (try? c.decodeIfPresent(Int.self, forKey: .sampleRate)) ?? 16000
        encoding = (try? c.decodeIfPresent(String.self, forKey: .encoding)) ?? "wav/pcm"
        bitDepth = (try? c.decodeIfPresent(Int.self, forKey: .bitDepth)) ?? 16
        channels = (try? c.decodeIfPresent(Int.self, forKey: .channels)) ?? 1
    }
}

// MARK: Requests

struct CreateAgentRequest: Encodable {
    var platform: Platform
    var task: String
    var title: String?
}

struct SendMessageRequest: Encodable {
    var text: String
}

struct CreateRunRequest: Encodable {
    var spec: String
    var platforms: [Platform]?
}

struct VoiceCommandRequest: Encodable {
    var text: String
}

struct EmptyBody: Encodable {}

/// Client-side aggregate of a `report.ready` event.
struct RunReport: Hashable, Sendable {
    var markdown: String
    var sources: [JSONValue]
    var devinSessionURL: String?
    var ts: Double
}
