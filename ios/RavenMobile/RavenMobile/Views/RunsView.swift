import SwiftUI

struct RunsView: View {
    @Environment(RavenStore.self) private var store
    @Binding var path: NavigationPath

    init(path: Binding<NavigationPath>) {
        self._path = path
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("Runs")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(Theme.text)
                    .padding(.top, 4)

                SectionLabel(text: "Specs")
                if store.specs.isEmpty {
                    Text(store.demoMode ? "No specs" : "No specs from \(store.serverURL)/specs")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.tertiary)
                }
                ForEach(store.specs) { spec in
                    SpecCard(spec: spec) { platforms in
                        Task {
                            if let run = await store.startRun(spec: spec.name, platforms: platforms) {
                                path.append(Route.run(run.id))
                            }
                        }
                    }
                }

                SectionLabel(text: "Recent runs")
                    .padding(.top, 10)
                if store.sortedRuns.isEmpty {
                    Text("No runs yet")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.tertiary)
                }
                ForEach(store.sortedRuns) { run in
                    NavigationLink(value: Route.run(run.id)) {
                        RunRow(run: run)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 40)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bg, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text("runs").font(Theme.mono(15, .semibold)).foregroundStyle(Theme.text)
            }
        }
        .task { await store.loadRunsAndSpecs() }
        .refreshable { await store.loadRunsAndSpecs() }
    }
}

struct SpecCard: View {
    let spec: SpecInfo
    let onStart: ([Platform]) -> Void
    @State private var selected: Set<Platform>
    @State private var starting = false

    init(spec: SpecInfo, onStart: @escaping ([Platform]) -> Void) {
        self.spec = spec
        self.onStart = onStart
        _selected = State(initialValue: Set(spec.platforms))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(spec.name)
                    .font(Theme.mono(14, .semibold))
                    .foregroundStyle(Theme.text)
                Spacer()
            }
            if !spec.description.isEmpty {
                Text(spec.description)
                    .font(.system(size: 13.5))
                    .foregroundStyle(Theme.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                ForEach(spec.platforms) { p in
                    let on = selected.contains(p)
                    Button {
                        if on { selected.remove(p) } else { selected.insert(p) }
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: p.symbol).font(.system(size: 11))
                            Text(p.displayName).font(Theme.mono(11, .medium))
                        }
                        .foregroundStyle(on ? Color.black : Theme.secondary)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 6)
                        .background(Capsule().fill(on ? Color.white : Theme.surface2))
                    }
                    .buttonStyle(.plain)
                }
                Spacer(minLength: 0)
            }
            Button {
                let ordered = Platform.allCases.filter { selected.contains($0) }
                starting = true
                onStart(ordered)
                Task {
                    try? await Task.sleep(nanoseconds: 800_000_000)
                    starting = false
                }
            } label: {
                HStack(spacing: 6) {
                    if starting {
                        PulsingDots(color: .black, size: 4)
                    } else {
                        Image(systemName: "play.fill").font(.system(size: 11))
                    }
                    Text("Run on \(selected.count) platform\(selected.count == 1 ? "" : "s")")
                        .font(Theme.mono(12.5, .semibold))
                }
                .foregroundStyle(.black)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .background(Capsule().fill(selected.isEmpty ? Theme.tertiary : Color.white))
            }
            .buttonStyle(.plain)
            .disabled(selected.isEmpty || starting)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Theme.surface))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
    }
}

struct RunRow: View {
    let run: Run
    @Environment(RavenStore.self) private var store

    init(run: Run) {
        self.run = run
    }

    var body: some View {
        let agents = store.agentsFor(run: run)
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    StatusIndicator(status: run.status.asAgentStatus)
                    Text(run.spec)
                        .font(Theme.mono(13.5, .semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                }
                HStack(spacing: 10) {
                    ForEach(agents) { a in
                        HStack(spacing: 4) {
                            Image(systemName: a.platform.symbol)
                                .font(.system(size: 11))
                                .foregroundStyle(Theme.secondary)
                            StatusIndicator(status: a.status, size: 5)
                        }
                    }
                    if agents.isEmpty {
                        ForEach(run.platforms) { p in
                            Image(systemName: p.symbol).font(.system(size: 11)).foregroundStyle(Theme.tertiary)
                        }
                    }
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                Text(run.status.rawValue)
                    .font(Theme.mono(11, .medium))
                    .foregroundStyle(Theme.color(for: run.status.asAgentStatus))
                Text(RelTime.short(run.createdAt))
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.tertiary)
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.tertiary)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.surface))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
    }
}

// MARK: Run detail

struct RunDetailView: View {
    let runID: String
    @Environment(RavenStore.self) private var store

    init(runID: String) {
        self.runID = runID
    }

    var body: some View {
        let run = store.runs[runID]
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let run {
                    let agents = store.agentsFor(run: run)
                    VStack(alignment: .leading, spacing: 6) {
                        Text(run.spec)
                            .font(Theme.mono(20, .semibold))
                            .foregroundStyle(Theme.text)
                        HStack(spacing: 8) {
                            StatusLabel(status: run.status.asAgentStatus)
                            Text("\(run.id) · \(RelTime.short(run.createdAt))")
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.tertiary)
                        }
                    }

                    SectionLabel(text: "Matrix")
                    RunMatrix(agents: agents)

                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                        ForEach(agents) { agent in
                            NavigationLink(value: Route.agent(agent.id)) {
                                PlatformTile(agent: agent)
                            }
                            .buttonStyle(.plain)
                        }
                    }

                    SectionLabel(text: "Report").padding(.top, 6)
                    if let report = store.reports[run.id] {
                        ReportCard(markdown: report.markdown, sources: report.sources, devinURL: report.devinSessionURL)
                    } else {
                        HStack(spacing: 10) {
                            PulsingDots(color: Theme.tertiary, size: 5)
                            Text(run.status == .running || run.status == .queued ? "Report will appear when all agents finish" : "No report")
                                .font(Theme.mono(12))
                                .foregroundStyle(Theme.tertiary)
                        }
                        .padding(.vertical, 8)
                    }
                } else {
                    Text("Run not found")
                        .font(Theme.mono(13))
                        .foregroundStyle(Theme.tertiary)
                        .padding(.top, 40)
                }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 40)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bg, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text("run").font(Theme.mono(15, .semibold)).foregroundStyle(Theme.text)
            }
        }
    }
}

/// Platform × status grid.
struct RunMatrix: View {
    let agents: [Agent]
    private let columns: [AgentStatus] = [.queued, .running, .passed, .failed]

    init(agents: [Agent]) {
        self.agents = agents
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                Text("").frame(width: 92, alignment: .leading)
                ForEach(columns, id: \.self) { s in
                    Text(s.label)
                        .font(Theme.mono(10, .medium))
                        .foregroundStyle(Theme.tertiary)
                        .frame(maxWidth: .infinity)
                }
            }
            .padding(.vertical, 8)
            Hairline()
            ForEach(agents) { agent in
                HStack(spacing: 0) {
                    HStack(spacing: 6) {
                        Image(systemName: agent.platform.symbol).font(.system(size: 12))
                        Text(agent.platform.displayName).font(Theme.mono(12, .medium))
                    }
                    .foregroundStyle(Theme.text)
                    .frame(width: 92, alignment: .leading)
                    ForEach(columns, id: \.self) { s in
                        ZStack {
                            if bucket(agent.status) == s {
                                StatusIndicator(status: agent.status, size: 9)
                            } else {
                                Circle().fill(Theme.surface3).frame(width: 4, height: 4)
                            }
                        }
                        .frame(maxWidth: .infinity, minHeight: 34)
                    }
                }
                Hairline()
            }
        }
        .padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.surface))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
    }

    private func bucket(_ s: AgentStatus) -> AgentStatus {
        switch s {
        case .error, .stopped: return .failed
        default: return s
        }
    }
}

struct PlatformTile: View {
    let agent: Agent

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScreenshotThumb(path: agent.lastScreenshot, width: nil, height: 92)
            HStack(spacing: 6) {
                Image(systemName: agent.platform.symbol).font(.system(size: 12))
                Text(agent.platform.displayName).font(.system(size: 13, weight: .semibold))
                Spacer()
                StatusIndicator(status: agent.status, size: 7)
            }
            .foregroundStyle(Theme.text)
            Text(agent.verdictReason ?? agent.status.label)
                .font(.system(size: 11.5))
                .foregroundStyle(Theme.secondary)
                .lineLimit(2)
                .frame(maxWidth: .infinity, minHeight: 28, alignment: .topLeading)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.surface))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
    }
}
