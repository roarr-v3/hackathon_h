import SwiftUI

struct AgentsListView: View {
    @Environment(RavenStore.self) private var store
    @State private var draft = ""

    var body: some View {
        let agents = store.sortedAgents
        ScrollView {
            LazyVStack(spacing: 0) {
                header
                    .padding(.horizontal, 16)
                    .padding(.top, 4)
                    .padding(.bottom, 14)

                if agents.isEmpty {
                    emptyState
                        .padding(.top, 80)
                } else {
                    ForEach(agents) { agent in
                        NavigationLink(value: Route.agent(agent.id)) {
                            AgentRow(agent: agent)
                        }
                        .buttonStyle(RowButtonStyle())
                        Hairline().padding(.leading, 64)
                    }
                }
            }
            .padding(.bottom, 20)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ComposerView(target: .home, placeholder: "Ask Raven to test something…", text: $draft)
        }
        .refreshable { await store.refresh() }
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bg, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                NavigationLink(value: Route.settings) {
                    ConnectionPill(state: store.connection)
                }
                .buttonStyle(.plain)
            }
            ToolbarItem(placement: .principal) {
                Text("raven")
                    .font(Theme.mono(15, .semibold))
                    .foregroundStyle(Theme.text)
            }
            ToolbarItem(placement: .topBarTrailing) {
                HStack(spacing: 14) {
                    NavigationLink(value: Route.runs) {
                        Image(systemName: "square.grid.2x2")
                    }
                    NavigationLink(value: Route.settings) {
                        Image(systemName: "gearshape")
                    }
                }
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(Theme.text)
            }
        }
    }

    private var header: some View {
        let c = store.counts
        return VStack(alignment: .leading, spacing: 6) {
            Text("Agents")
                .font(.system(size: 30, weight: .semibold))
                .foregroundStyle(Theme.text)
            HStack(spacing: 12) {
                stat(c.running, "running", Theme.text, running: true)
                stat(c.passed, "passed", Theme.green)
                stat(c.failed, "failed", Theme.red)
                Spacer()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func stat(_ n: Int, _ label: String, _ color: Color, running: Bool = false) -> some View {
        HStack(spacing: 5) {
            if running && n > 0 {
                PulsingDots(color: .white, size: 3.5, spacing: 2)
            } else {
                Circle().fill(color).frame(width: 6, height: 6)
            }
            Text("\(n) \(label)")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.secondary)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            PulsingDots(color: Theme.tertiary, size: 7, spacing: 6)
            Text("No agents yet")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(Theme.text)
            Text(store.demoMode
                 ? "Say “run the geohash test on Windows” or start a run."
                 : "Waiting for raven-core at \(store.serverURL)")
                .font(.system(size: 14))
                .foregroundStyle(Theme.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)
            NavigationLink(value: Route.runs) {
                Text("Start a run")
                    .font(Theme.mono(13, .medium))
                    .foregroundStyle(.black)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 9)
                    .background(Capsule().fill(Color.white))
            }
            .buttonStyle(.plain)
        }
        .frame(maxWidth: .infinity)
    }
}

struct RowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed ? Theme.surface : Color.clear)
    }
}

struct AgentRow: View {
    let agent: Agent
    @Environment(RavenStore.self) private var store

    init(agent: Agent) {
        self.agent = agent
    }

    var body: some View {
        let preview = store.preview(for: agent)
        let activity = store.lastActivity(agent)
        HStack(alignment: .top, spacing: 12) {
            PlatformIcon(platform: agent.platform, size: 38)
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(agent.title)
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    TimelineView(.periodic(from: .now, by: 20)) { ctx in
                        Text(RelTime.short(activity, now: ctx.date))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.tertiary)
                    }
                }
                HStack(alignment: .center, spacing: 7) {
                    StatusIndicator(status: agent.status)
                        .frame(width: 18, alignment: .leading)
                    Text(agent.platform.displayName)
                        .font(Theme.mono(11, .medium))
                        .foregroundStyle(Theme.secondary)
                    if !agent.worker.isEmpty {
                        Text("· \(agent.worker)")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.tertiary)
                            .lineLimit(1)
                    }
                }
                Text(preview.0)
                    .font(preview.1 ? Theme.mono(12.5) : .system(size: 14))
                    .foregroundStyle(Theme.secondary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            ScreenshotThumb(path: agent.lastScreenshot, width: 66, height: 44)
                .padding(.top, 2)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
        .contentShape(Rectangle())
    }
}
