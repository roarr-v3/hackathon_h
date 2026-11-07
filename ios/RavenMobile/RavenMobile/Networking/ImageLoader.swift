import SwiftUI
import UIKit

/// Loads artifact screenshots with the bearer token attached (AsyncImage can't add headers).
actor ImageLoader {
    static let shared = ImageLoader()

    private let cache = NSCache<NSURL, UIImage>()
    private var inflight: [URL: Task<UIImage?, Never>] = [:]

    init() {
        cache.countLimit = 120
    }

    func image(for url: URL, token: String?) async -> UIImage? {
        if let hit = cache.object(forKey: url as NSURL) { return hit }
        if let running = inflight[url] { return await running.value }
        let task = Task<UIImage?, Never> {
            var req = URLRequest(url: url, timeoutInterval: 20)
            if let token, !token.isEmpty {
                req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }
            guard let result = try? await URLSession.shared.data(for: req),
                  let http = result.1 as? HTTPURLResponse, (200..<300).contains(http.statusCode),
                  let img = UIImage(data: result.0) else { return nil }
            return img
        }
        inflight[url] = task
        let img = await task.value
        inflight[url] = nil
        if let img { cache.setObject(img, forKey: url as NSURL) }
        return img
    }
}

struct RemoteImage: View {
    let url: URL?
    let token: String?
    var contentMode: ContentMode = .fill

    @State private var image: UIImage?
    @State private var failed = false

    init(url: URL?, token: String?, contentMode: ContentMode = .fill) {
        self.url = url
        self.token = token
        self.contentMode = contentMode
    }

    var body: some View {
        ZStack {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: contentMode)
            } else {
                Rectangle()
                    .fill(Theme.surface2)
                    .overlay {
                        if failed {
                            Image(systemName: "photo")
                                .font(.system(size: 14))
                                .foregroundStyle(Theme.tertiary)
                        } else {
                            PulsingDots(color: Theme.tertiary, size: 4)
                        }
                    }
            }
        }
        .task(id: url) {
            guard let url else { failed = true; return }
            failed = false
            let img = await ImageLoader.shared.image(for: url, token: token)
            image = img
            failed = img == nil
        }
    }
}
