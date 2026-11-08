import Foundation
import Observation
import SwiftUI

enum ComposerTarget: Hashable {
    case home
    case agent(String)
}

/// Single source of truth for the app: settings, agents, events, runs, voice.
@MainActor
@Observable
final class RavenStore {
    private enum Keys {
        static let server = "raven.serverURL"
        static let demo = "raven.demoMode"
        static let token = "raven.token"
    }

    // MARK: Settings
    var serverURL: String = "http://localhost:8787" {
        didSet { UserDefaults.standard.set(serverURL, forKey: Keys.server) }
    }
    var token: String = "" {
        didSet { Keychain.set(token, for: Keys.token) }
    }
    var demoMode: Bool = false {
        didSet { UserDefaults.standard.set(demoMode, forKey: Keys.demo) }
    }

    // MARK: State
    var connection: ConnectionState = .offline
    var agents: [String: Agent] = [:]
    var eventsByAgent: [String: [RavenEvent]] = [:]
    var runs: [String: Run] = [:]
    var reports: [String: RunReport] = [:]
    var specs: [SpecInfo] = []
    var banner: String?
    var voiceTarget: ComposerTarget?

    let voice = VoiceRecorder()

    @ObservationIgnored private var seen = Set<String>()
    @ObservationIgnored private var deleted = Set<String>()
    @ObservationIgnored private let socket = EventSocket()
    @ObservationIgnored private var demo: DemoEngine?
    @ObservationIgnored private var started = false
    @ObservationIgnored private var loadingAgents = Set<String>()
    @ObservationIgnored private var bannerTask: Task<Void, Never>?

    init() {
        let defaults = UserDefaults.standard
        serverURL = defaults.string(forKey: Keys.server) ?? "http://localhost:8787"
        token = Keychain.get(Keys.token) ?? ""
        demoMode = defaults.object(forKey: Keys.demo) as? Bool ?? false
        // Launch arguments for screenshots / CI: `-demo YES|NO`, `-server http://...`
        if defaults.object(forKey: "demo") != nil { demoMode = defaults.bool(forKey: "demo") }
        if let forcedServer = defaults.string(forKey: "server") { serverURL = forcedServer }

        socket.onEvent = { [weak self] event in self?.ingest(event) }
        socket.onState = { [weak self] state in
            guard let self, !self.demoMode, self.connection != state else { return }
            let wasLive = self.connection == .live
            self.connection = state
            if state == .live && !wasLive {
                Task { await self.refresh() }
            }
        }
        voice.onFinal = { [weak self] _ in
            guard let self else { return }
            Task { await self.finishVoice() }
        }
    }

    var api: APIClient? { APIClient(server: serverURL, token: token) }

    // MARK: Lifecycle

    func start() {
        guard !started else { return }
        started = true
        connect()
    }

    @discardableResult
    func pair(with link: URL) async -> String? {
        guard link.scheme == "raven",
              let items = URLComponents(url: link, resolvingAgainstBaseURL: false)?.queryItems else { return nil }
        let token = items.first { $0.name == "token" }?.value ?? ""
        let candidates = items.filter { $0.name == "url" }.compactMap(\.value)
        var reachable: String?
        for candidate in candidates {
            if let api = APIClient(server: candidate, token: token), await Self.answers(api) {
                reachable = candidate
                break
            }
        }
        if let chosen = reachable ?? candidates.first {
            serverURL = chosen
            self.token = token
            demoMode = false
            reconnect()
        }
        return reachable
    }

    private static func answers(_ api: APIClient) async -> Bool {
        await withTaskGroup(of: Bool.self) { group in
            group.addTask { (try? await api.health()) ?? false }
            group.addTask {
                try? await Task.sleep(for: .seconds(3))
                return false
            }
            let first = await group.next() ?? false
            group.cancelAll()
            return first
        }
    }

    /// Tear down and reconnect with the current settings.
    func reconnect() {
        socket.disconnect()
        demo?.stop()
        demo = nil
        agents = [:]
        eventsByAgent = [:]
        runs = [:]
        reports = [:]
        specs = []
        seen = []
        connect()
    }

    private func connect() {
        if demoMode {
            connection = .demo
            let engine = DemoEngine(store: self)
            demo = engine
            engine.start()
            return
        }
        guard let api, let wsURL = api.eventsSocketURL else {
            connection = .error("Invalid server URL")
            return
        }
        connection = .connecting
        socket.connect(to: wsURL)
        Task { await refresh() }
        #if !targetEnvironment(simulator)
        if ["localhost", "127.0.0.1"].contains(api.baseURL.host ?? "") {
            flash("Not paired: scan the QR in Raven Agent → Settings → Raven")
        }
        #endif
    }

    func refresh() async {
        if demoMode { return }
        guard let api else { return }
        if let list = try? await api.agents() {
            for a in list { upsert(a) }
        }
        if let list = try? await api.runs() {
            for r in list { runs[r.id] = r }
        }
        if let list = try? await api.specs() { specs = list }
    }

    func loadAgent(_ id: String) async {
        if demoMode { return }
        guard let api, !loadingAgents.contains(id) else { return }
        loadingAgents.insert(id)
        defer { loadingAgents.remove(id) }
        do {
            let detail = try await api.agent(id)
            upsert(detail.agent)
            for e in detail.events { ingest(e) }
        } catch {
            flash("Couldn't load agent: \(error.localizedDescription)")
        }
    }

    func loadRunsAndSpecs() async {
        if demoMode { return }
        guard let api else { return }
        if let list = try? await api.specs() { specs = list }
        if let list = try? await api.runs() {
            for r in list { runs[r.id] = r }
        }
    }

    // MARK: Event ingestion

    func upsert(_ agent: Agent) {
        guard !deleted.contains(agent.id) else { return }
        if var existing = agents[agent.id] {
            // Keep a newer screenshot we may have learned from events.
            let shot = agent.lastScreenshot ?? existing.lastScreenshot
            existing = agent
            existing.lastScreenshot = shot
            agents[agent.id] = existing
        } else {
            agents[agent.id] = agent
        }
        if let runID = agent.runId, var run = runs[runID], !run.agentIds.contains(agent.id) {
            run.agentIds.append(agent.id)
            runs[runID] = run
        }
    }

    func ingest(_ e: RavenEvent) {
        guard !seen.contains(e.id) else { return }
        seen.insert(e.id)
        if let id = e.agentId, deleted.contains(id) { return }

        switch e.type {
        case EventType.agentCreated:
            if let a = e.agent { upsert(a) }
        case EventType.agentStatus:
            if let id = e.agentId, let s = e.statusValue { agents[id]?.status = AgentStatus(raw: s) }
        case EventType.agentScreenshot:
            if let id = e.agentId, let url = e.screenshotURL { agents[id]?.lastScreenshot = url }
        case EventType.agentVerdict:
            if let id = e.agentId {
                if let passed = e.passed { agents[id]?.status = passed ? .passed : .failed }
                agents[id]?.verdictReason = e.reason
            }
        case EventType.runCreated:
            if let r = e.run { runs[r.id] = r }
        case EventType.runStatus:
            if let id = e.runId, let s = e.statusValue { runs[id]?.status = RunStatus(raw: s) }
        case EventType.reportReady:
            if let id = e.runId ?? e.agentId {
                reports[id] = RunReport(
                    markdown: e.markdown ?? "",
                    sources: e.data["linkup_sources"]?.array ?? [],
                    devinSessionURL: e.data["devin_session_url"]?.string,
                    ts: e.ts
                )
            }
        default:
            break
        }

        guard let agentID = e.agentId else { return }
        if agents[agentID] == nil && !demoMode {
            Task { await loadAgent(agentID) }
        }
        var list = eventsByAgent[agentID] ?? []
        // Drop optimistic local user messages once the server echoes them.
        if e.type == EventType.agentMessage, e.role == "user", let text = e.text {
            list.removeAll { $0.id.hasPrefix("local_") && $0.text == text }
        }
        list.append(e)
        if list.count > 1, list[list.count - 2].ts > e.ts {
            list.sort { $0.ts < $1.ts }
        }
        eventsByAgent[agentID] = list
    }

    // MARK: Derived

    func lastActivity(_ agent: Agent) -> Double {
        eventsByAgent[agent.id]?.last?.ts ?? agent.createdAt
    }

    var sortedAgents: [Agent] {
        agents.values.sorted { lastActivity($0) > lastActivity($1) }
    }

    var sortedRuns: [Run] {
        runs.values.sorted { $0.createdAt > $1.createdAt }
    }

    func agentsFor(run: Run) -> [Agent] {
        var ids = run.agentIds
        for a in agents.values where a.runId == run.id && !ids.contains(a.id) { ids.append(a.id) }
        let list = ids.compactMap { agents[$0] }
        let order = Platform.allCases
        return list.sorted { (order.firstIndex(of: $0.platform) ?? 0) < (order.firstIndex(of: $1.platform) ?? 0) }
    }

    /// Preview line for the thread list: (text, isMonospace).
    func preview(for agent: Agent) -> (String, Bool) {
        let events = eventsByAgent[agent.id] ?? []
        for e in events.reversed() {
            switch e.type {
            case EventType.agentMessage:
                if let t = e.text, !t.isEmpty { return (e.role == "user" ? "You: \(t)" : t, false) }
            case EventType.agentAction:
                return ("› " + e.actionLine, true)
            case EventType.agentVerdict:
                return ((e.passed == true ? "Passed — " : "Failed — ") + (e.reason ?? ""), false)
            default:
                continue
            }
        }
        if let r = agent.verdictReason { return (r, false) }
        return (agent.task, false)
    }

    func artifactURL(_ path: String?) -> URL? {
        guard let path, !path.isEmpty, let api else { return nil }
        return api.url(path)
    }

    var counts: (running: Int, passed: Int, failed: Int) {
        var r = 0, p = 0, f = 0
        for a in agents.values {
            switch a.status {
            case .running, .queued: r += 1
            case .passed: p += 1
            case .failed, .error: f += 1
            case .stopped: break
            }
        }
        return (r, p, f)
    }

    // MARK: Actions

    func send(_ raw: String, to target: ComposerTarget) async {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        switch target {
        case .agent(let id):
            let local = RavenEvent(id: "local_" + UUID().uuidString, type: EventType.agentMessage, agentId: id,
                                   data: ["role": "user", "text": .string(text)])
            if demoMode {
                demo?.steer(agentID: id, text: text)
                return
            }
            ingest(local)
            do {
                try await api?.sendMessage(agentID: id, text: text)
            } catch {
                flash("Send failed: \(error.localizedDescription)")
            }
        case .home:
            if demoMode {
                demo?.command(text)
                flash("Raven: on it")
                return
            }
            do {
                let response = try await api?.voiceCommand(text)
                if let agent = response?["agent"]?.decode(Agent.self) ?? response?.decode(Agent.self) {
                    upsert(agent)
                }
                if let msg = response?["message"]?.string ?? response?["reply"]?.string {
                    flash(msg)
                } else {
                    flash("Sent to Raven")
                }
            } catch {
                flash("Command failed: \(error.localizedDescription)")
            }
        }
    }

    func delete(agentIDs: Set<String>) async {
        var failed = 0
        for id in agentIDs {
            if !demoMode {
                do {
                    try await api?.deleteAgent(id)
                } catch {
                    failed += 1
                    continue
                }
            }
            deleted.insert(id)
            agents[id] = nil
            eventsByAgent[id] = nil
        }
        if failed > 0 {
            flash("Could not delete \(failed) chat\(failed == 1 ? "" : "s"). Stop running agents first.")
        }
    }

    func stop(agentID: String) async {
        if demoMode {
            demo?.stopAgent(agentID)
            return
        }
        do {
            try await api?.stopAgent(agentID)
            agents[agentID]?.status = .stopped
        } catch {
            flash("Stop failed: \(error.localizedDescription)")
        }
    }

    @discardableResult
    func startRun(spec: String, platforms: [Platform]) async -> Run? {
        if demoMode {
            return demo?.startRun(spec: spec, platforms: platforms)
        }
        do {
            guard let api else { return nil }
            let run = try await api.createRun(CreateRunRequest(spec: spec, platforms: platforms.isEmpty ? nil : platforms))
            runs[run.id] = run
            return run
        } catch {
            flash("Run failed to start: \(error.localizedDescription)")
            return nil
        }
    }

    func testConnection() async -> String {
        guard let api else { return "Invalid server URL" }
        do {
            let ok = try await api.health()
            return ok ? "Healthy — \(api.baseURL.absoluteString)" : "Server responded but not ok"
        } catch {
            return "Unreachable: \(error.localizedDescription)"
        }
    }

    func flash(_ text: String) {
        banner = text
        bannerTask?.cancel()
        bannerTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_600_000_000)
            guard !Task.isCancelled else { return }
            self?.banner = nil
        }
    }

    // MARK: Voice

    func isRecording(_ target: ComposerTarget) -> Bool {
        voiceTarget == target && voice.phase != .idle
    }

    func startVoice(target: ComposerTarget) async {
        guard voice.phase == .idle else { return }
        voiceTarget = target
        voice.beginConnecting()
        if demoMode {
            voice.startDemo(phrase: DemoEngine.voicePhrase(for: target, store: self))
            return
        }
        do {
            guard let api else { throw APIError(message: "Invalid server URL") }
            let session = try await api.voiceSession()
            try await voice.start(session: session)
        } catch {
            voice.fail(error.localizedDescription)
            voiceTarget = nil
        }
    }

    func finishVoice() async {
        guard let target = voiceTarget, voice.phase == .recording || voice.phase == .connecting else { return }
        let text = await voice.stop()
        voiceTarget = nil
        if !text.isEmpty { await send(text, to: target) }
    }

    func cancelVoice() {
        voice.cancel()
        voiceTarget = nil
    }
}
