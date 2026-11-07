import Foundation

struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Thin REST client for raven-core (see AGENTS.md).
struct APIClient: Sendable {
    let baseURL: URL
    let token: String?

    init?(server: String, token: String?) {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return nil }
        if !s.contains("://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        guard let url = URL(string: s), url.host != nil else { return nil }
        self.baseURL = url
        let t = token?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.token = (t?.isEmpty ?? true) ? nil : t
    }

    func url(_ path: String) -> URL {
        if path.hasPrefix("http://") || path.hasPrefix("https://"), let u = URL(string: path) { return u }
        let p = path.hasPrefix("/") ? path : "/" + path
        return URL(string: baseURL.absoluteString + p) ?? baseURL
    }

    /// ws(s)://host/ws/events?token=...
    var eventsSocketURL: URL? {
        guard var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { return nil }
        comps.scheme = (comps.scheme == "https") ? "wss" : "ws"
        var path = comps.path
        while path.hasSuffix("/") { path.removeLast() }
        comps.path = path + "/ws/events"
        if let token { comps.queryItems = [URLQueryItem(name: "token", value: token)] }
        return comps.url
    }

    func authorize(_ req: inout URLRequest) {
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
    }

    @discardableResult
    func send(_ method: String, _ path: String, body: (any Encodable)? = nil, timeout: TimeInterval = 15) async throws -> Data {
        var req = URLRequest(url: url(path), timeoutInterval: timeout)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        authorize(&req)
        if let body {
            req.httpBody = try JSONEncoder().encode(body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw APIError(message: "No HTTP response") }
        guard (200..<300).contains(http.statusCode) else {
            let snippet = String(decoding: data.prefix(200), as: UTF8.self)
            throw APIError(message: "HTTP \(http.statusCode) \(method) \(path): \(snippet)")
        }
        return data
    }

    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        let data = try await send("GET", path)
        return try JSONDecoder().decode(T.self, from: data)
    }

    func post<T: Decodable>(_ path: String, body: (any Encodable)? = nil, as type: T.Type = T.self) async throws -> T {
        let data = try await send("POST", path, body: body ?? EmptyBody())
        return try JSONDecoder().decode(T.self, from: data)
    }

    // MARK: Endpoints

    func health() async throws -> Bool {
        let v: JSONValue = try await get("/health")
        return v["ok"]?.bool ?? true
    }

    func agents() async throws -> [Agent] { try await get("/agents") }
    func agent(_ id: String) async throws -> AgentDetail { try await get("/agents/\(id)") }
    func createAgent(_ req: CreateAgentRequest) async throws -> Agent { try await post("/agents", body: req) }
    func sendMessage(agentID: String, text: String) async throws {
        try await send("POST", "/agents/\(agentID)/messages", body: SendMessageRequest(text: text))
    }
    func stopAgent(_ id: String) async throws { try await send("POST", "/agents/\(id)/stop", body: EmptyBody()) }
    func runs() async throws -> [Run] { try await get("/runs") }
    func run(_ id: String) async throws -> Run { try await get("/runs/\(id)") }
    func createRun(_ req: CreateRunRequest) async throws -> Run { try await post("/runs", body: req) }
    func specs() async throws -> [SpecInfo] { try await get("/specs") }
    func voiceSession() async throws -> VoiceSession { try await post("/voice/session", body: EmptyBody()) }
    func voiceCommand(_ text: String) async throws -> JSONValue {
        try await post("/voice/command", body: VoiceCommandRequest(text: text))
    }
}
