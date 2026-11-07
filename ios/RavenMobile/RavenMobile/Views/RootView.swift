import SwiftUI

enum Route: Hashable {
    case agent(String)
    case runs
    case run(String)
    case settings
}

struct RootView: View {
    @Environment(RavenStore.self) private var store
    @State private var path = NavigationPath()
    @State private var didDeepLink = false

    var body: some View {
        NavigationStack(path: $path) {
            AgentsListView()
                .navigationDestination(for: Route.self) { route in
                    switch route {
                    case .agent(let id): AgentThreadView(agentID: id)
                    case .runs: RunsView(path: $path)
                    case .run(let id): RunDetailView(runID: id)
                    case .settings: SettingsView()
                    }
                }
        }
        .tint(.white)
        .overlay(alignment: .top) {
            if let banner = store.banner {
                Text(banner)
                    .font(Theme.mono(12, .medium))
                    .foregroundStyle(Theme.text)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(Capsule().fill(Theme.surface3))
                    .overlay(Capsule().strokeBorder(Theme.stroke, lineWidth: 0.5))
                    .padding(.top, 6)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: store.banner)
        .task {
            store.start()
            deepLinkIfRequested()
        }
    }

    /// Launch argument `-screen thread|runs|run|settings` (handy for simulator screenshots).
    private func deepLinkIfRequested() {
        guard !didDeepLink, let screen = UserDefaults.standard.string(forKey: "screen") else { return }
        didDeepLink = true
        switch screen {
        case "thread":
            let id = UserDefaults.standard.string(forKey: "agent") ?? "agt_demo_win"
            path.append(Route.agent(id))
        case "runs":
            path.append(Route.runs)
        case "run":
            path.append(Route.run(UserDefaults.standard.string(forKey: "run") ?? "run_demo_geohash"))
        case "settings":
            path.append(Route.settings)
        default:
            break
        }
    }
}
