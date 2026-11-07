import SwiftUI

enum Theme {
    static let bg = Color.black
    static let surface = Color(white: 0.075)
    static let surface2 = Color(white: 0.115)
    static let surface3 = Color(white: 0.16)
    static let stroke = Color(white: 0.15)
    static let text = Color.white
    static let secondary = Color(white: 0.62)
    static let tertiary = Color(white: 0.40)
    static let green = Color(red: 0.27, green: 0.86, blue: 0.47)
    static let red = Color(red: 1.0, green: 0.36, blue: 0.36)
    static let amber = Color(red: 1.0, green: 0.76, blue: 0.32)

    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    static func color(for status: AgentStatus) -> Color {
        switch status {
        case .queued: return tertiary
        case .running: return .white
        case .passed: return green
        case .failed, .error: return red
        case .stopped: return tertiary
        }
    }
}

enum RelTime {
    static func short(_ ts: Double, now: Date = Date()) -> String {
        let s = max(0, now.timeIntervalSince1970 - ts)
        if s < 30 { return "now" }
        if s < 3600 { return "\(max(1, Int(s / 60)))m" }
        if s < 86400 { return "\(Int(s / 3600))h" }
        return "\(Int(s / 86400))d"
    }

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    static func clock(_ ts: Double) -> String {
        clockFormatter.string(from: Date(timeIntervalSince1970: ts))
    }
}

struct Hairline: View {
    var body: some View {
        Rectangle().fill(Theme.stroke).frame(height: 0.5)
    }
}

struct SectionLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(Theme.mono(11, .medium))
            .tracking(1.2)
            .foregroundStyle(Theme.tertiary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}
