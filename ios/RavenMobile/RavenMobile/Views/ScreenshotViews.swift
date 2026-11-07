import SwiftUI

struct ScreenshotRef: Identifiable, Hashable {
    let url: String
    var caption: String = ""
    var aspect: CGFloat = 1.6
    var id: String { url }
}

/// Renders an artifact screenshot (remote PNG) or a synthetic demo screen.
struct ScreenshotView: View {
    let path: String?
    var contentMode: ContentMode = .fill
    @Environment(RavenStore.self) private var store

    init(path: String?, contentMode: ContentMode = .fill) {
        self.path = path
        self.contentMode = contentMode
    }

    var body: some View {
        if let path, path.hasPrefix("demo://") {
            DemoScreen(path: path)
        } else {
            RemoteImage(url: store.artifactURL(path), token: store.token, contentMode: contentMode)
        }
    }
}

/// Thumbnail of an agent's last screenshot, used in the thread list.
struct ScreenshotThumb: View {
    let path: String?
    /// nil = fill available width
    var width: CGFloat? = 64
    var height: CGFloat = 42

    var body: some View {
        Group {
            if path != nil {
                ScreenshotView(path: path)
            } else {
                Rectangle().fill(Theme.surface)
                    .overlay(Image(systemName: "rectangle.dashed").font(.system(size: 12)).foregroundStyle(Theme.tertiary))
            }
        }
        .frame(width: width, height: height)
        .frame(maxWidth: width == nil ? CGFloat.infinity : nil)
        .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
    }
}

// MARK: Fullscreen + zoom

struct FullscreenScreenshot: View {
    let ref: ScreenshotRef
    @Environment(\.dismiss) private var dismiss

    init(ref: ScreenshotRef) {
        self.ref = ref
    }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            ZoomableContainer {
                ScreenshotView(path: ref.url, contentMode: .fit)
                    .aspectRatio(ref.aspect, contentMode: .fit)
            }
            .ignoresSafeArea()

            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 36, height: 36)
                    .background(Circle().fill(Theme.surface3))
            }
            .padding(16)

            if !ref.caption.isEmpty {
                VStack {
                    Spacer()
                    Text(ref.caption)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.secondary)
                        .padding(.bottom, 24)
                }
                .frame(maxWidth: .infinity)
                .allowsHitTesting(false)
            }
        }
        .statusBarHidden()
    }
}

struct ZoomableContainer<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    @State private var scale: CGFloat = 1
    @State private var lastScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var lastOffset: CGSize = .zero

    var body: some View {
        GeometryReader { geo in
            content
                .frame(width: geo.size.width, height: geo.size.height)
                .scaleEffect(scale)
                .offset(offset)
                .contentShape(Rectangle())
                .gesture(
                    MagnifyGesture()
                        .onChanged { value in
                            scale = min(6, max(1, lastScale * value.magnification))
                        }
                        .onEnded { _ in
                            lastScale = scale
                            if scale <= 1.01 { reset() }
                        }
                )
                .simultaneousGesture(
                    DragGesture()
                        .onChanged { value in
                            guard scale > 1 else { return }
                            offset = CGSize(width: lastOffset.width + value.translation.width,
                                            height: lastOffset.height + value.translation.height)
                        }
                        .onEnded { _ in lastOffset = offset }
                )
                .onTapGesture(count: 2) {
                    withAnimation(.spring(duration: 0.3)) {
                        if scale > 1 { reset() } else { scale = 2.5; lastScale = 2.5 }
                    }
                }
        }
    }

    private func reset() {
        withAnimation(.spring(duration: 0.3)) {
            scale = 1
            lastScale = 1
            offset = .zero
            lastOffset = .zero
        }
    }
}

// MARK: Demo screen renderer

/// Synthetic "screenshot" of a sandbox desktop running bitchat, keyed by `demo://<platform>/<phase>`.
/// phase 0 desktop · 1 app open · 2 channel joined · 3 typing · 4 sent · 9 stuck sending
struct DemoScreen: View {
    let path: String

    private var parts: (Platform, Int) {
        let comps = path.replacingOccurrences(of: "demo://", with: "").split(separator: "/")
        let p = comps.first.flatMap { Platform(rawValue: String($0)) } ?? .macos
        let n = comps.count > 1 ? Int(comps[1]) ?? 0 : 0
        return (p, n)
    }

    var body: some View {
        let platform = parts.0
        let phase = parts.1
        GeometryReader { geo in
            let w = geo.size.width
            let h = geo.size.height
            ZStack {
                wallpaper(platform)
                if platform == .ios {
                    iosScreen(phase: phase, w: w, h: h)
                } else {
                    desktop(platform: platform, phase: phase, w: w, h: h)
                }
            }
            .frame(width: w, height: h)
            .clipped()
        }
    }

    private func wallpaper(_ p: Platform) -> LinearGradient {
        let colors: [Color]
        switch p {
        case .macos: colors = [Color(red: 0.18, green: 0.12, blue: 0.42), Color(red: 0.55, green: 0.25, blue: 0.55), Color(red: 0.95, green: 0.55, blue: 0.45)]
        case .linux: colors = [Color(red: 0.18, green: 0.05, blue: 0.15), Color(red: 0.45, green: 0.12, blue: 0.25), Color(red: 0.85, green: 0.35, blue: 0.15)]
        case .windows: colors = [Color(red: 0.02, green: 0.10, blue: 0.30), Color(red: 0.05, green: 0.35, blue: 0.75), Color(red: 0.35, green: 0.70, blue: 0.95)]
        case .ios: colors = [Color(red: 0.05, green: 0.05, blue: 0.12), Color(red: 0.15, green: 0.20, blue: 0.40)]
        }
        return LinearGradient(colors: colors, startPoint: .topLeading, endPoint: .bottomTrailing)
    }

    private func chatLines(phase: Int) -> [(String, Color)] {
        let g = Color(red: 0.3, green: 1.0, blue: 0.45)
        var lines: [(String, Color)] = [("* connected to 4 nostr relays", g.opacity(0.6))]
        if phase >= 2 {
            lines += [
                ("* joined #u09tv (geohash · 3 online)", g.opacity(0.6)),
                ("<anon4821> anyone in paris?", g),
                ("<satoshi> gm from the hackathon", g),
            ]
        }
        if phase == 4 { lines.append(("<raven> hello from raven", .white)) }
        if phase == 9 {
            lines.append(("<raven> hello from raven  (sending…)", Color.white.opacity(0.5)))
            lines.append(("! relay timeout: wss://relay.damus.io", Color(red: 1, green: 0.4, blue: 0.4)))
        }
        return lines
    }

    @ViewBuilder
    private func bitchatWindow(platform: Platform, phase: Int, w: CGFloat, h: CGFloat) -> some View {
        let fs = max(4, w * 0.018)
        VStack(spacing: 0) {
            // Title bar
            HStack(spacing: w * 0.006) {
                if platform == .macos {
                    Circle().fill(Color.red.opacity(0.9)).frame(width: fs * 0.8, height: fs * 0.8)
                    Circle().fill(Color.yellow.opacity(0.9)).frame(width: fs * 0.8, height: fs * 0.8)
                    Circle().fill(Color.green.opacity(0.9)).frame(width: fs * 0.8, height: fs * 0.8)
                }
                Spacer()
                Text("bitchat").font(.system(size: fs * 0.9, weight: .semibold)).foregroundStyle(.white.opacity(0.75))
                Spacer()
                if platform == .windows {
                    Text("—  ☐  ✕").font(.system(size: fs * 0.8)).foregroundStyle(.white.opacity(0.7))
                }
            }
            .padding(.horizontal, w * 0.012)
            .frame(height: fs * 1.9)
            .background(Color(white: 0.16))

            VStack(alignment: .leading, spacing: fs * 0.45) {
                ForEach(Array(chatLines(phase: phase).enumerated()), id: \.offset) { _, line in
                    Text(line.0)
                        .font(.system(size: fs, design: .monospaced))
                        .foregroundStyle(line.1)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                HStack {
                    Text(phase == 3 ? "hello from raven▍" : (phase == 1 ? "/j #u09tv" : "type a message…"))
                        .font(.system(size: fs, design: .monospaced))
                        .foregroundStyle(phase == 3 ? Color.white : Color.white.opacity(0.35))
                    Spacer()
                }
                .padding(fs * 0.5)
                .background(RoundedRectangle(cornerRadius: fs * 0.4).stroke(Color.white.opacity(0.2), lineWidth: 1))
            }
            .padding(fs * 0.9)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(Color(white: 0.06))
        }
        .clipShape(RoundedRectangle(cornerRadius: fs * 0.6, style: .continuous))
        .shadow(color: .black.opacity(0.5), radius: fs, y: fs * 0.4)
    }

    @ViewBuilder
    private func desktop(platform: Platform, phase: Int, w: CGFloat, h: CGFloat) -> some View {
        let fs = max(4, w * 0.018)
        ZStack {
            if phase >= 1 {
                bitchatWindow(platform: platform, phase: phase, w: w, h: h)
                    .frame(width: w * 0.62, height: h * 0.64)
                    .offset(y: platform == .linux ? h * 0.02 : -h * 0.03)
            }
            VStack(spacing: 0) {
                if platform != .windows {
                    // menu / top bar
                    HStack {
                        Text(platform == .macos ? "  Finder   File   Edit   View" : "  Activities")
                            .font(.system(size: fs * 0.8, weight: .medium))
                        Spacer()
                        Text("14:02  ").font(.system(size: fs * 0.8, weight: .medium))
                    }
                    .foregroundStyle(.white.opacity(0.9))
                    .frame(height: fs * 1.6)
                    .background(Color.black.opacity(platform == .linux ? 0.85 : 0.25))
                }
                Spacer()
                if platform == .macos {
                    HStack(spacing: w * 0.01) {
                        ForEach(0..<8, id: \.self) { i in
                            RoundedRectangle(cornerRadius: w * 0.008, style: .continuous)
                                .fill(i == 4 ? Color(red: 0.2, green: 0.9, blue: 0.4) : Color.white.opacity(0.35 + Double(i % 3) * 0.12))
                                .frame(width: w * 0.04, height: w * 0.04)
                        }
                    }
                    .padding(w * 0.008)
                    .background(RoundedRectangle(cornerRadius: w * 0.014, style: .continuous).fill(Color.white.opacity(0.18)))
                    .padding(.bottom, h * 0.02)
                } else if platform == .windows {
                    HStack(spacing: w * 0.012) {
                        Spacer()
                        ForEach(0..<6, id: \.self) { i in
                            RoundedRectangle(cornerRadius: 2)
                                .fill(i == 3 ? Color(red: 0.2, green: 0.9, blue: 0.4) : Color.white.opacity(0.55))
                                .frame(width: w * 0.022, height: w * 0.022)
                        }
                        Spacer()
                    }
                    .frame(height: h * 0.065)
                    .background(Color(white: 0.1).opacity(0.92))
                }
            }
            if platform == .linux {
                HStack {
                    VStack(spacing: h * 0.025) {
                        ForEach(0..<6, id: \.self) { i in
                            RoundedRectangle(cornerRadius: 4)
                                .fill(i == 3 ? Color(red: 0.2, green: 0.9, blue: 0.4) : Color.white.opacity(0.4))
                                .frame(width: w * 0.032, height: w * 0.032)
                        }
                    }
                    .padding(.vertical, h * 0.03)
                    .padding(.horizontal, w * 0.008)
                    .background(Color.black.opacity(0.55))
                    Spacer()
                }
                .padding(.top, fs * 1.6)
            }
        }
    }

    @ViewBuilder
    private func iosScreen(phase: Int, w: CGFloat, h: CGFloat) -> some View {
        let fs = max(4, w * 0.04)
        if phase == 0 {
            VStack {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: w * 0.06), count: 4), spacing: w * 0.07) {
                    ForEach(0..<16, id: \.self) { i in
                        RoundedRectangle(cornerRadius: w * 0.04, style: .continuous)
                            .fill(i == 9 ? Color(red: 0.2, green: 0.9, blue: 0.4) : Color.white.opacity(0.2 + Double(i % 4) * 0.08))
                            .aspectRatio(1, contentMode: .fit)
                    }
                }
                .padding(.horizontal, w * 0.07)
                .padding(.top, h * 0.09)
                Spacer()
            }
        } else {
            VStack(alignment: .leading, spacing: fs * 0.6) {
                Text("bitchat").font(.system(size: fs * 1.2, weight: .bold, design: .monospaced)).foregroundStyle(.white)
                    .padding(.top, h * 0.07)
                ForEach(Array(chatLines(phase: phase).enumerated()), id: \.offset) { _, line in
                    Text(line.0).font(.system(size: fs * 0.85, design: .monospaced)).foregroundStyle(line.1).lineLimit(2)
                }
                Spacer()
                Text(phase == 3 ? "hello from raven▍" : "type a message…")
                    .font(.system(size: fs * 0.85, design: .monospaced))
                    .foregroundStyle(.white.opacity(phase == 3 ? 1 : 0.4))
                    .padding(fs * 0.6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: fs).stroke(Color.white.opacity(0.25)))
                    .padding(.bottom, h * 0.05)
            }
            .padding(.horizontal, w * 0.06)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(white: 0.04))
        }
    }
}
