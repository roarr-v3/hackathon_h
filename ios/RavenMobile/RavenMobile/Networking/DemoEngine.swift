import Foundation

/// Offline simulator that speaks the exact same Event contract as raven-core, so the
/// whole UI is demo-able without a backend. Screenshots use `demo://<platform>/<phase>`
/// URLs which `ScreenshotView` renders as a synthetic desktop.
@MainActor
final class DemoEngine {
    private enum Step {
        case message(String)
        case action(String, [String: JSONValue], String?)
        case screenshot(Int)
        case verdict
    }

    private weak var store: RavenStore?
    private var loop: Task<Void, Never>?
    private var progress: [String: Int] = [:]
    private var willFail: Set<String> = []
    private var customTask: [String: String] = [:]
    private var reportedRuns: Set<String> = []

    init(store: RavenStore) {
        self.store = store
    }

    func start() {
        store?.specs = DemoEngine.specs
        seed()
        loop = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_200_000_000)
                guard !Task.isCancelled else { return }
                self?.tick()
            }
        }
    }

    func stop() {
        loop?.cancel()
        loop = nil
    }

    static let specs: [SpecInfo] = [
        SpecInfo(name: "bitchat-geohash-chat",
                 description: "Join geohash channel #u09tv over Nostr, send a message, verify it appears on every client.",
                 platforms: Platform.allCases),
        SpecInfo(name: "bitchat-private-dm",
                 description: "Open a private DM with a nearby peer and exchange two messages.",
                 platforms: [.macos, .linux, .ios]),
        SpecInfo(name: "app-launch-smoke",
                 description: "Launch bitchat cold, accept onboarding, reach the main timeline in < 10 s.",
                 platforms: Platform.allCases),
    ]

    static func voicePhrase(for target: ComposerTarget, store: RavenStore) -> String {
        switch target {
        case .home:
            return "Run the geohash chat test on Windows and Linux"
        case .agent(let id):
            if store.agents[id]?.status.isActive == true {
                return "Try clicking the relay settings and switch to a different relay"
            }
            return "Run it again but wait five seconds before sending the message"
        }
    }

    // MARK: Script

    private func script(for agentID: String, platform: Platform) -> [Step] {
        let fails = willFail.contains(agentID)
        let icon: (Int, Int) = {
            switch platform {
            case .macos: return (412, 768)
            case .windows: return (188, 1052)
            case .linux: return (36, 214)
            case .ios: return (96, 612)
            }
        }()
        let input: (Int, Int) = platform == .ios ? (180, 780) : (640, 706)
        let place: String
        switch platform {
        case .windows: place = "taskbar"
        case .ios: place = "home screen"
        default: place = "dock"
        }
        let iconArgs: [String: JSONValue] = ["x": .number(Double(icon.0)), "y": .number(Double(icon.1))]
        let inputArgs: [String: JSONValue] = ["x": .number(Double(input.0)), "y": .number(Double(input.1))]
        var steps: [Step] = [
            .message("Starting on \(platform.displayName). Taking a screenshot to see where we are."),
            .screenshot(0),
            .action("click", iconArgs, "The bitchat icon is in the \(place)."),
            .action("wait", ["seconds": 2], nil),
            .screenshot(1),
            .message("bitchat is open and connected to relays. Joining geohash channel #u09tv."),
            .action("click", inputArgs, "Focus the message input."),
            .action("type_text", ["text": "/j #u09tv"], nil),
            .action("key", ["combo": "enter"], nil),
            .screenshot(2),
            .action("type_text", ["text": "hello from raven"], "Channel joined, 3 peers online. Sending the probe message."),
            .screenshot(3),
            .action("key", ["combo": "enter"], nil),
        ]
        if fails {
            steps += [
                .screenshot(9),
                .message("The message is stuck in \"sending…\". The relay never acknowledged it — retrying once."),
                .action("key", ["combo": "enter"], "Retry send."),
                .action("wait", ["seconds": 10], nil),
                .verdict,
            ]
        } else {
            steps += [
                .screenshot(4),
                .message("\"hello from raven\" is visible in #u09tv with my nickname. Message delivered."),
                .verdict,
            ]
        }
        return steps
    }

    private func screenshotURL(_ platform: Platform, _ phase: Int) -> String {
        "demo://\(platform.rawValue)/\(phase)"
    }

    private func size(_ platform: Platform) -> (Int, Int) {
        platform == .ios ? (390, 844) : (1280, 800)
    }

    // MARK: Emit

    private func emit(_ type: String, agent: String? = nil, run: String? = nil, data: [String: JSONValue] = [:],
                      ts: Double = Date().timeIntervalSince1970) {
        store?.ingest(RavenEvent(type: type, ts: ts, runId: run, agentId: agent, data: data))
    }

    private func emitStep(_ step: Step, agent: Agent, ts: Double) {
        switch step {
        case .message(let text):
            emit(EventType.agentMessage, agent: agent.id, run: agent.runId, data: ["role": "agent", "text": .string(text)], ts: ts)
        case .action(let name, let args, let thought):
            var data: [String: JSONValue] = ["action": .string(name), "args": .object(args)]
            if let thought { data["thought"] = .string(thought) }
            emit(EventType.agentAction, agent: agent.id, run: agent.runId, data: data, ts: ts)
        case .screenshot(let phase):
            let (w, h) = size(agent.platform)
            emit(EventType.agentScreenshot, agent: agent.id, run: agent.runId,
                 data: ["url": .string(screenshotURL(agent.platform, phase)), "width": .number(Double(w)), "height": .number(Double(h))],
                 ts: ts)
        case .verdict:
            let fails = willFail.contains(agent.id)
            let reason = fails
                ? "Message never left \"sending…\": Nostr relay wss://relay.damus.io did not return OK within 30 s."
                : "Message \"hello from raven\" appeared in #u09tv and was echoed by peers."
            emit(EventType.agentVerdict, agent: agent.id, run: agent.runId, data: ["passed": .bool(!fails), "reason": .string(reason)], ts: ts)
            emit(EventType.agentStatus, agent: agent.id, run: agent.runId, data: ["status": .string(fails ? "failed" : "passed")], ts: ts + 0.01)
        }
    }

    private func createAgent(id: String, run: String?, platform: Platform, title: String, task: String,
                             status: AgentStatus = .running, ts: Double) -> Agent {
        let worker: String = {
            switch platform {
            case .macos: return "cua-lume-macos-15"
            case .linux: return "cua-docker-ubuntu"
            case .windows: return "spacesd-home-pc"
            case .ios: return "sim-iphone-17"
            }
        }()
        let agent = Agent(id: id, runId: run, platform: platform, worker: worker, title: title, task: task,
                          status: .queued, createdAt: ts)
        emit(EventType.agentCreated, agent: id, run: run, data: ["agent": JSONValue(encoding: agent) ?? .null], ts: ts)
        emit(EventType.agentStatus, agent: id, run: run, data: ["status": .string(status.rawValue)], ts: ts + 0.05)
        return agent
    }

    // MARK: Seed

    private func seed() {
        let now = Date().timeIntervalSince1970
        let task = "Open bitchat, join geohash channel #u09tv, send \"hello from raven\" and verify it is delivered."

        // An older finished standalone agent.
        let old = createAgent(id: "agt_demo_old", run: nil, platform: .linux, title: "Onboarding flow",
                              task: "Launch bitchat for the first time and complete onboarding.", ts: now - 3600)
        replay(old, upTo: 99, start: now - 3590, spacing: 4)

        // The live run.
        let runID = "run_demo_geohash"
        let platforms = Platform.allCases
        var run = Run(id: runID, spec: "bitchat-geohash-chat", platforms: platforms, status: .running, agentIds: [], createdAt: now - 120)
        let ids = ["agt_demo_ios", "agt_demo_win", "agt_demo_linux", "agt_demo_mac"]
        run.agentIds = ids
        emit(EventType.runCreated, run: runID, data: ["run": JSONValue(encoding: run) ?? .null], ts: now - 120)
        willFail.insert("agt_demo_win")

        let ios = createAgent(id: ids[0], run: runID, platform: .ios, title: "bitchat · geohash chat", task: task, ts: now - 118)
        replay(ios, upTo: 99, start: now - 115, spacing: 5)

        let win = createAgent(id: ids[1], run: runID, platform: .windows, title: "bitchat · geohash chat", task: task, ts: now - 118)
        replay(win, upTo: 12, start: now - 110, spacing: 6)

        let linux = createAgent(id: ids[2], run: runID, platform: .linux, title: "bitchat · geohash chat", task: task, ts: now - 118)
        replay(linux, upTo: 4, start: now - 60, spacing: 9)

        let mac = createAgent(id: ids[3], run: runID, platform: .macos, title: "bitchat · geohash chat", task: task, ts: now - 118)
        replay(mac, upTo: 9, start: now - 70, spacing: 6)
    }

    /// Emit the first `upTo` steps instantly with back-dated timestamps.
    private func replay(_ agent: Agent, upTo: Int, start: Double, spacing: Double) {
        let steps = script(for: agent.id, platform: agent.platform)
        let n = min(upTo, steps.count)
        for i in 0..<n {
            emitStep(steps[i], agent: agent, ts: start + Double(i) * spacing)
        }
        progress[agent.id] = n
    }

    // MARK: Tick

    private func tick() {
        guard let store else { return }
        for agent in store.agents.values where agent.status == .running {
            let steps = script(for: agent.id, platform: agent.platform)
            let i = progress[agent.id] ?? 0
            guard i < steps.count else { continue }
            // Stagger agents a little so they don't all move in lockstep.
            if Int.random(in: 0..<10) < 3 { continue }
            emitStep(steps[i], agent: agent, ts: Date().timeIntervalSince1970)
            progress[agent.id] = i + 1
        }
        finishRunsIfDone()
    }

    private func finishRunsIfDone() {
        guard let store else { return }
        for run in store.runs.values where run.status == .running && !reportedRuns.contains(run.id) {
            let agents = store.agentsFor(run: run)
            guard !agents.isEmpty, agents.allSatisfy({ !$0.status.isActive }) else { continue }
            reportedRuns.insert(run.id)
            let failed = agents.filter { $0.status == .failed || $0.status == .error }
            let status: RunStatus = failed.isEmpty ? .passed : .failed
            emit(EventType.runStatus, run: run.id, data: ["status": .string(status.rawValue)])
            var sources: [JSONValue] = []
            var devin: JSONValue = .null
            if !failed.isEmpty {
                let s1: JSONValue = .object([
                    "title": .string("Nostr NIP-01: OK messages and relay acknowledgements"),
                    "url": .string("https://github.com/nostr-protocol/nips/blob/master/01.md"),
                ])
                let s2: JSONValue = .object([
                    "title": .string("Windows Firewall filtering outbound WebSockets for unsigned apps"),
                    "url": .string("https://learn.microsoft.com/windows/security/operating-system-security/network-security/windows-firewall/"),
                ])
                sources = [s1, s2]
                devin = .string("https://app.devin.ai/sessions/demo-raven-fix")
            }
            var data: [String: JSONValue] = [:]
            data["markdown"] = .string(report(run: run, agents: agents))
            data["linkup_sources"] = .array(sources)
            data["devin_session_url"] = devin
            emit(EventType.reportReady, run: run.id, data: data)
        }
    }

    private func report(run: Run, agents: [Agent]) -> String {
        let passed = agents.filter { $0.status == .passed }.count
        var md = "# \(run.spec)\n\n**\(passed)/\(agents.count) platforms passed**\n\n"
        for a in agents {
            md += "- **\(a.platform.displayName)** — \(a.status.rawValue)"
            if let r = a.verdictReason { md += ": \(r)" }
            md += "\n"
        }
        let failed = agents.filter { $0.status == .failed }
        if !failed.isEmpty {
            md += "\n## Diagnosis\n\nOn **Windows** the client opens the relay socket but never receives `OK` for the EVENT. "
            md += "Linkup research points to the outbound WebSocket being filtered when the app runs unsigned.\n"
            md += "\n## Proposed fix (Devin)\n\nAdd fallback relays and surface a send timeout in `NostrRelayManager`. "
            md += "Devin opened a session with a patch.\n"
        }
        return md
    }

    // MARK: User actions

    func steer(agentID: String, text: String) {
        guard let store, let agent = store.agents[agentID] else { return }
        emit(EventType.agentMessage, agent: agentID, run: agent.runId, data: ["role": "user", "text": .string(text)])
        let wasActive = agent.status.isActive
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_100_000_000)
            guard let self, let store = self.store, let agent = store.agents[agentID] else { return }
            if wasActive {
                self.emit(EventType.agentMessage, agent: agentID, run: agent.runId,
                          data: ["role": "agent", "text": .string("Got it — adjusting: \(text.prefix(80))")])
            } else {
                self.emit(EventType.agentMessage, agent: agentID, run: agent.runId,
                          data: ["role": "agent", "text": "Understood. Re-running from the app launch with your instruction."])
                self.willFail.remove(agentID)
                self.progress[agentID] = 4
                self.emit(EventType.agentStatus, agent: agentID, run: agent.runId, data: ["status": "running"])
            }
        }
    }

    func stopAgent(_ id: String) {
        guard let agent = store?.agents[id] else { return }
        emit(EventType.agentMessage, agent: id, run: agent.runId, data: ["role": "system", "text": "Stopped by user"])
        emit(EventType.agentStatus, agent: id, run: agent.runId, data: ["status": "stopped"])
    }

    func command(_ text: String) {
        let lower = text.lowercased()
        var platforms = Platform.allCases.filter { p in
            switch p {
            case .macos: return lower.contains("mac")
            case .linux: return lower.contains("linux") || lower.contains("ubuntu")
            case .windows: return lower.contains("windows")
            case .ios: return lower.contains("ios") || lower.contains("iphone")
            }
        }
        if lower.contains("test") || lower.contains("run") {
            if platforms.isEmpty { platforms = Platform.allCases }
            _ = startRun(spec: "bitchat-geohash-chat", platforms: platforms)
            return
        }
        let platform = platforms.first ?? .macos
        let id = "agt_demo_" + UUID().uuidString.prefix(6).lowercased()
        let title = text.count > 40 ? String(text.prefix(40)) + "…" : text
        let now = Date().timeIntervalSince1970
        let agent = createAgent(id: id, run: nil, platform: platform, title: title, task: text, ts: now)
        emit(EventType.agentMessage, agent: agent.id, data: ["role": "user", "text": .string(text)], ts: now + 0.1)
        progress[id] = 0
    }

    func startRun(spec: String, platforms: [Platform]) -> Run {
        let now = Date().timeIntervalSince1970
        let runID = "run_demo_" + UUID().uuidString.prefix(6).lowercased()
        let chosen = platforms.isEmpty ? Platform.allCases : platforms
        let ids = chosen.map { "agt_\($0.rawValue)_" + UUID().uuidString.prefix(6).lowercased() }
        let run = Run(id: runID, spec: spec, platforms: chosen, status: .running, agentIds: ids, createdAt: now)
        emit(EventType.runCreated, run: runID, data: ["run": JSONValue(encoding: run) ?? .null], ts: now)
        let task = DemoEngine.specs.first { $0.name == spec }?.description ?? spec
        for (platform, id) in zip(chosen, ids) {
            if platform == .windows && spec == "bitchat-geohash-chat" { willFail.insert(id) }
            _ = createAgent(id: id, run: runID, platform: platform, title: spec, task: task, ts: now + 0.1)
            progress[id] = 0
        }
        return run
    }
}
