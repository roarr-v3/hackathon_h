import SwiftUI

/// The "thinking" dots: three dots breathing in sequence.
struct PulsingDots: View {
    var color: Color = .white
    var size: CGFloat = 4
    var spacing: CGFloat = 3

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: spacing) {
                ForEach(0..<3, id: \.self) { i in
                    let v = (sin((t * 2.4 - Double(i) * 0.45) * .pi) + 1) / 2
                    Circle()
                        .fill(color)
                        .frame(width: size, height: size)
                        .opacity(0.25 + 0.75 * v)
                        .scaleEffect(0.7 + 0.3 * v)
                }
            }
        }
    }
}

/// Small status indicator: running = pulsing dots, otherwise a coloured dot.
struct StatusIndicator: View {
    let status: AgentStatus
    var size: CGFloat = 7

    var body: some View {
        switch status {
        case .running:
            PulsingDots(color: .white, size: size * 0.62, spacing: size * 0.35)
        case .queued:
            PulsingDots(color: Theme.tertiary, size: size * 0.62, spacing: size * 0.35)
        default:
            Circle()
                .fill(Theme.color(for: status))
                .frame(width: size, height: size)
                .shadow(color: Theme.color(for: status).opacity(status == .stopped ? 0 : 0.6), radius: 3)
        }
    }
}

struct StatusLabel: View {
    let status: AgentStatus

    var body: some View {
        HStack(spacing: 6) {
            StatusIndicator(status: status, size: 6)
            Text(status.label)
                .font(Theme.mono(11, .medium))
                .foregroundStyle(status == .running ? Theme.text : Theme.color(for: status))
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Capsule().fill(Theme.surface2))
    }
}

struct PlatformIcon: View {
    let platform: Platform
    var size: CGFloat = 36

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
            .fill(Theme.surface2)
            .overlay(
                RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
                    .strokeBorder(Theme.stroke, lineWidth: 0.5)
            )
            .overlay(
                Image(systemName: platform.symbol)
                    .font(.system(size: size * 0.44, weight: .medium))
                    .foregroundStyle(Theme.text)
            )
            .frame(width: size, height: size)
    }
}

struct ConnectionPill: View {
    let state: ConnectionState

    var body: some View {
        HStack(spacing: 6) {
            switch state {
            case .connecting:
                PulsingDots(color: Theme.amber, size: 3.5, spacing: 2)
            default:
                Circle().fill(color).frame(width: 6, height: 6)
            }
            Text(state.label)
                .font(Theme.mono(11, .medium))
                .foregroundStyle(Theme.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(Capsule().fill(Theme.surface2))
        .overlay(Capsule().strokeBorder(Theme.stroke, lineWidth: 0.5))
    }

    private var color: Color {
        switch state {
        case .live: return Theme.green
        case .demo: return Theme.amber
        case .connecting: return Theme.amber
        case .offline, .error: return Theme.red
        }
    }
}

/// Simple level meter for the recording composer.
struct LevelBars: View {
    let level: Double

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 2.5) {
                ForEach(0..<5, id: \.self) { i in
                    let wobble = (sin(t * 9 + Double(i) * 1.3) + 1) / 2
                    let h = 4 + 16 * min(1, level * (0.55 + 0.45 * wobble))
                    Capsule()
                        .fill(Theme.text)
                        .frame(width: 3, height: h)
                }
            }
            .frame(height: 20)
        }
    }
}
