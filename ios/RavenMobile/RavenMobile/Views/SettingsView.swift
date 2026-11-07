import SwiftUI

struct SettingsView: View {
    @Environment(RavenStore.self) private var store
    @State private var server = ""
    @State private var token = ""
    @State private var testResult: String?
    @State private var testing = false
    @State private var loaded = false

    private var dirty: Bool { server != store.serverURL || token != store.token }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Settings")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(Theme.text)
                    .padding(.top, 4)

                group(label: "Connection") {
                    row {
                        HStack {
                            Text("Status").foregroundStyle(Theme.text)
                            Spacer()
                            ConnectionPill(state: store.connection)
                        }
                    }
                    Hairline()
                    row {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Server URL").font(Theme.mono(11)).foregroundStyle(Theme.tertiary)
                            TextField("http://localhost:8787", text: $server)
                                .font(Theme.mono(14))
                                .foregroundStyle(Theme.text)
                                .keyboardType(.URL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                        }
                    }
                    Hairline()
                    row {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Bearer token (Keychain)").font(Theme.mono(11)).foregroundStyle(Theme.tertiary)
                            SecureField("optional — RAVEN_TOKEN", text: $token)
                                .font(Theme.mono(14))
                                .foregroundStyle(Theme.text)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                        }
                    }
                }

                HStack(spacing: 10) {
                    pillButton(dirty ? "Save & reconnect" : "Reconnect", filled: true) {
                        store.serverURL = server.trimmingCharacters(in: .whitespacesAndNewlines)
                        store.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
                        store.reconnect()
                        testResult = nil
                    }
                    pillButton(testing ? "Testing…" : "Test /health", filled: false) {
                        testing = true
                        let s = server
                        let t = token
                        Task {
                            let probe = APIClient(server: s, token: t)
                            if let probe {
                                do {
                                    let ok = try await probe.health()
                                    testResult = ok ? "✓ healthy — \(probe.baseURL.absoluteString)" : "responded, not ok"
                                } catch {
                                    testResult = "✗ \(error.localizedDescription)"
                                }
                            } else {
                                testResult = "✗ invalid URL"
                            }
                            testing = false
                        }
                    }
                }
                if let testResult {
                    Text(testResult)
                        .font(Theme.mono(11.5))
                        .foregroundStyle(testResult.hasPrefix("✓") ? Theme.green : Theme.red)
                }

                group(label: "Demo") {
                    row {
                        Toggle(isOn: Binding(
                            get: { store.demoMode },
                            set: { newValue in
                                store.demoMode = newValue
                                store.reconnect()
                            }
                        )) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Demo mode").foregroundStyle(Theme.text)
                                Text("Simulated agents, screenshots and voice — no backend needed.")
                                    .font(.system(size: 12))
                                    .foregroundStyle(Theme.tertiary)
                            }
                        }
                        .tint(Theme.green)
                    }
                }

                group(label: "Voice") {
                    row {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Gladia live STT").foregroundStyle(Theme.text)
                            Text("Hold the mic to talk, release to send. Tap for hands-free: sends on the first final transcript. Audio is streamed as 16 kHz mono PCM straight to Gladia via POST /voice/session.")
                                .font(.system(size: 12))
                                .foregroundStyle(Theme.tertiary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }

                group(label: "About") {
                    row {
                        HStack {
                            Text("RavenMobile").foregroundStyle(Theme.text)
                            Spacer()
                            Text(appVersion).font(Theme.mono(12)).foregroundStyle(Theme.tertiary)
                        }
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 40)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg.ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bg, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text("settings").font(Theme.mono(15, .semibold)).foregroundStyle(Theme.text)
            }
        }
        .onAppear {
            guard !loaded else { return }
            loaded = true
            server = store.serverURL
            token = store.token
        }
    }

    private var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "v\(v) (\(b))"
    }

    private func group<Content: View>(label: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: label)
            VStack(spacing: 0) {
                content()
            }
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.surface))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
        }
    }

    private func row<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .font(.system(size: 15))
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func pillButton(_ title: String, filled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(Theme.mono(12.5, .semibold))
                .foregroundStyle(filled ? Color.black : Theme.text)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(Capsule().fill(filled ? Color.white : Theme.surface2))
        }
        .buttonStyle(.plain)
    }
}
