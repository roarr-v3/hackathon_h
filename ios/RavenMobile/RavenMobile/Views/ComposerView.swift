import SwiftUI

/// Big pill composer with text input + push-to-talk / tap-to-talk mic.
/// - Hold mic: records while held, submits on release.
/// - Tap mic: hands-free; submits on the first final transcript (or tap stop).
struct ComposerView: View {
    let target: ComposerTarget
    let placeholder: String
    @Binding var text: String

    @Environment(RavenStore.self) private var store
    @FocusState private var focused: Bool
    @State private var pressStart: Date?
    @State private var stopOnRelease = false

    init(target: ComposerTarget, placeholder: String, text: Binding<String>) {
        self.target = target
        self.placeholder = placeholder
        self._text = text
    }

    private var recording: Bool { store.isRecording(target) }
    private var hasText: Bool { !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        VStack(spacing: 6) {
            if let err = store.voice.errorMessage, store.voiceTarget == nil || store.voiceTarget == target {
                Text(err)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 18)
            }
            HStack(alignment: .bottom, spacing: 8) {
                Group {
                    if recording {
                        recordingField
                    } else {
                        TextField(placeholder, text: $text, axis: .vertical)
                            .lineLimit(1...5)
                            .font(.system(size: 16))
                            .foregroundStyle(Theme.text)
                            .tint(.white)
                            .focused($focused)
                            .submitLabel(.send)
                    }
                }
                .frame(minHeight: 44)
                .padding(.leading, 18)
                .frame(maxWidth: .infinity, alignment: .leading)

                trailingButtons
                    .padding(.trailing, 6)
                    .padding(.bottom, 6)
            }
            .padding(.top, 2)
            .background(
                RoundedRectangle(cornerRadius: 28, style: .continuous)
                    .fill(Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 28, style: .continuous)
                    .strokeBorder(recording ? Color.white.opacity(0.35) : Theme.stroke, lineWidth: recording ? 1 : 0.5)
            )
            .animation(.easeOut(duration: 0.18), value: recording)
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 6)
        .background(
            LinearGradient(colors: [Color.black.opacity(0), Color.black, Color.black], startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
        )
    }

    private var recordingField: some View {
        HStack(alignment: .center, spacing: 10) {
            if store.voice.phase == .connecting {
                PulsingDots(color: Theme.secondary, size: 5)
            } else {
                LevelBars(level: store.voice.level)
            }
            let live = store.voice.liveText
            Text(live.isEmpty ? (store.voice.phase == .connecting ? "Connecting…" : "Listening…") : live)
                .font(.system(size: 16))
                .foregroundStyle(live.isEmpty ? Theme.tertiary : Theme.text)
                .lineLimit(4)
                .frame(maxWidth: .infinity, alignment: .leading)
                .animation(.easeOut(duration: 0.12), value: live)
        }
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private var trailingButtons: some View {
        HStack(spacing: 6) {
            if recording {
                Button {
                    store.cancelVoice()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Theme.secondary)
                        .frame(width: 36, height: 36)
                        .background(Circle().fill(Theme.surface3))
                }
                .buttonStyle(.plain)
            }
            if hasText && !recording {
                Button(action: submitText) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(.black)
                        .frame(width: 40, height: 40)
                        .background(Circle().fill(Color.white))
                }
                .buttonStyle(.plain)
                .transition(.scale.combined(with: .opacity))
            } else {
                micButton
            }
        }
    }

    private var micButton: some View {
        let pressed = pressStart != nil
        return ZStack {
            if recording {
                Circle()
                    .stroke(Color.white.opacity(0.25), lineWidth: 6)
                    .scaleEffect(1 + store.voice.level * 0.35)
            }
            Circle()
                .fill(recording ? Theme.red : Color.white)
            Image(systemName: recording ? "stop.fill" : "mic.fill")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(recording ? Color.white : Color.black)
        }
        .frame(width: 40, height: 40)
        .scaleEffect(pressed ? 0.9 : 1)
        .animation(.easeOut(duration: 0.12), value: pressed)
        .contentShape(Circle())
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    guard pressStart == nil else { return }
                    pressStart = Date()
                    focused = false
                    if recording {
                        stopOnRelease = true
                    } else {
                        stopOnRelease = false
                        Task { await store.startVoice(target: target) }
                    }
                }
                .onEnded { _ in
                    let held = Date().timeIntervalSince(pressStart ?? Date())
                    pressStart = nil
                    if stopOnRelease || held > 0.45 {
                        Task { await store.finishVoice() }
                    } else {
                        store.voice.autoSubmitOnFinal = true
                    }
                }
        )
        .sensoryFeedback(.impact(weight: .medium), trigger: recording)
        .accessibilityLabel(recording ? "Stop recording" : "Start voice input")
    }

    private func submitText() {
        let t = text
        text = ""
        Task { await store.send(t, to: target) }
    }
}
