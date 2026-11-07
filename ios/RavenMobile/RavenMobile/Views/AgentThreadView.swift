import SwiftUI

struct AgentThreadView: View {
    let agentID: String
    @Environment(RavenStore.self) private var store
    @State private var draft = ""
    @State private var fullscreen: ScreenshotRef?
    @State private var confirmStop = false

    init(agentID: String) {
        self.agentID = agentID
    }

    var body: some View {
        let agent = store.agents[agentID]
        let events = (store.eventsByAgent[agentID] ?? []).filter { $0.type != EventType.agentCreated }
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if let agent {
                        ThreadHeader(agent: agent)
                            .padding(.bottom, 6)
                    }
                    ForEach(events) { event in
                        TimelineRow(event: event, platform: agent?.platform ?? .macos) { ref in
                            fullscreen = ref
                        }
                        .id(event.id)
                    }
                    if agent?.status.isActive == true {
                        HStack(spacing: 10) {
                            PulsingDots(color: Theme.secondary, size: 6, spacing: 4)
                            Text(agent?.status == .queued ? "queued" : "working")
                                .font(Theme.mono(12))
                                .foregroundStyle(Theme.tertiary)
                        }
                        .padding(.vertical, 6)
                        .padding(.leading, 4)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 12)
            }
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: events.count) {
                withAnimation(.easeOut(duration: 0.25)) {
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
            }
        }
        .background(Theme.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ComposerView(target: .agent(agentID),
                         placeholder: agent?.status.isActive == true ? "Steer this agent…" : "Reply to re-run…",
                         text: $draft)
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bg, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .principal) {
                VStack(spacing: 1) {
                    Text(agent?.title ?? "Agent")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    if let agent {
                        HStack(spacing: 5) {
                            Image(systemName: agent.platform.symbol).font(.system(size: 9))
                            Text(agent.platform.displayName.lowercased() + " · " + agent.status.label)
                                .font(Theme.mono(10))
                        }
                        .foregroundStyle(Theme.secondary)
                    }
                }
            }
            if agent?.status.isActive == true {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        confirmStop = true
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "stop.fill").font(.system(size: 10))
                            Text("Stop").font(Theme.mono(12, .semibold))
                        }
                        .foregroundStyle(Theme.red)
                    }
                }
            }
        }
        .confirmationDialog("Stop this agent?", isPresented: $confirmStop, titleVisibility: .visible) {
            Button("Stop agent", role: .destructive) {
                Task { await store.stop(agentID: agentID) }
            }
        }
        .task { await store.loadAgent(agentID) }
        .fullScreenCover(item: $fullscreen) { ref in
            FullscreenScreenshot(ref: ref)
                .environment(store)
        }
    }
}

struct ThreadHeader: View {
    let agent: Agent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                PlatformIcon(platform: agent.platform, size: 30)
                VStack(alignment: .leading, spacing: 2) {
                    Text(agent.platform.displayName)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Theme.text)
                    Text(agent.worker.isEmpty ? agent.id : agent.worker)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.tertiary)
                }
                Spacer()
                StatusLabel(status: agent.status)
            }
            Text(agent.task)
                .font(.system(size: 14))
                .foregroundStyle(Theme.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.surface))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
    }
}
