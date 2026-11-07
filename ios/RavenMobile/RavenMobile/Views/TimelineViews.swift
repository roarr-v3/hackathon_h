import SwiftUI

struct TimelineRow: View {
    let event: RavenEvent
    let platform: Platform
    var onOpenScreenshot: (ScreenshotRef) -> Void = { _ in }

    var body: some View {
        switch event.type {
        case EventType.agentMessage:
            MessageBubble(role: event.role, text: event.text ?? "", pending: event.id.hasPrefix("local_"))
        case EventType.agentAction:
            ActionLine(line: event.actionLine, thought: event.thought, ts: event.ts)
        case EventType.agentScreenshot:
            ScreenshotCard(event: event, platform: platform, onOpen: onOpenScreenshot)
        case EventType.agentVerdict:
            VerdictCard(passed: event.passed ?? false, reason: event.reason ?? "")
        case EventType.agentStatus:
            StatusLine(status: AgentStatus(raw: event.statusValue ?? "queued"), ts: event.ts)
        case EventType.reportReady:
            if let md = event.markdown, !md.isEmpty {
                ReportCard(markdown: md, sources: event.data["linkup_sources"]?.array ?? [],
                           devinURL: event.data["devin_session_url"]?.string)
            }
        default:
            EmptyView()
        }
    }
}

struct MessageBubble: View {
    let role: String
    let text: String
    var pending = false

    var body: some View {
        switch role {
        case "user":
            HStack {
                Spacer(minLength: 48)
                Text(text)
                    .font(.system(size: 15))
                    .foregroundStyle(Theme.text)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Theme.surface3))
                    .opacity(pending ? 0.6 : 1)
            }
        case "system":
            Text(text)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.tertiary)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 2)
        default:
            HStack(alignment: .top) {
                Text(text)
                    .font(.system(size: 15))
                    .foregroundStyle(Theme.text)
                    .lineSpacing(2)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Theme.surface))
                    .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
                    .textSelection(.enabled)
                Spacer(minLength: 40)
            }
        }
    }
}

struct ActionLine: View {
    let line: String
    let thought: String?
    let ts: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let thought {
                Text(thought)
                    .font(.system(size: 12.5))
                    .italic()
                    .foregroundStyle(Theme.tertiary)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("›")
                    .font(Theme.mono(13, .bold))
                    .foregroundStyle(Theme.tertiary)
                Text(line)
                    .font(Theme.mono(12.5))
                    .foregroundStyle(Theme.secondary)
                    .lineLimit(3)
                Spacer(minLength: 8)
                Text(RelTime.clock(ts))
                    .font(Theme.mono(9.5))
                    .foregroundStyle(Theme.tertiary.opacity(0.7))
            }
        }
        .padding(.leading, 6)
        .padding(.vertical, 1)
        .overlay(alignment: .leading) {
            Rectangle().fill(Theme.stroke).frame(width: 1).offset(x: -2)
        }
    }
}

struct ScreenshotCard: View {
    let event: RavenEvent
    let platform: Platform
    let onOpen: (ScreenshotRef) -> Void

    private var aspect: CGFloat {
        if let w = event.imageWidth, let h = event.imageHeight, w > 0, h > 0 { return CGFloat(w / h) }
        return platform == .ios ? 390.0 / 844.0 : 1.6
    }

    var body: some View {
        let url = event.screenshotURL ?? ""
        let caption = "screenshot · \(Int(event.imageWidth ?? 0))×\(Int(event.imageHeight ?? 0)) · \(RelTime.clock(event.ts))"
        let maxWidth: CGFloat = aspect < 1 ? 170 : 300
        VStack(alignment: .leading, spacing: 5) {
            Button {
                onOpen(ScreenshotRef(url: url, caption: caption, aspect: aspect))
            } label: {
                ScreenshotView(path: url, contentMode: .fit)
                    .aspectRatio(aspect, contentMode: .fit)
                    .frame(maxWidth: maxWidth)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
            }
            .buttonStyle(.plain)
            Text(caption)
                .font(Theme.mono(10))
                .foregroundStyle(Theme.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct StatusLine: View {
    let status: AgentStatus
    let ts: Double

    var body: some View {
        HStack(spacing: 8) {
            Hairline().frame(maxWidth: 40)
            StatusIndicator(status: status, size: 5)
            Text(status.label)
                .font(Theme.mono(10.5))
                .foregroundStyle(Theme.tertiary)
            Hairline().frame(maxWidth: 40)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 2)
    }
}

struct VerdictCard: View {
    let passed: Bool
    let reason: String

    var body: some View {
        let color = passed ? Theme.green : Theme.red
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: passed ? "checkmark.circle.fill" : "xmark.octagon.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(color)
                Text(passed ? "PASSED" : "FAILED")
                    .font(Theme.mono(13, .bold))
                    .tracking(1.5)
                    .foregroundStyle(color)
                Spacer()
                Text("verdict")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.tertiary)
            }
            if !reason.isEmpty {
                Text(reason)
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.text.opacity(0.9))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(color.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(color.opacity(0.45), lineWidth: 1))
        .padding(.top, 4)
    }
}
