import SwiftUI

/// Tiny block-level markdown renderer (headings, bullets, code fences, paragraphs);
/// inline styling via AttributedString.
struct MarkdownView: View {
    let markdown: String

    private enum Block: Hashable {
        case heading(Int, String)
        case bullet(String)
        case code(String)
        case paragraph(String)
    }

    private var blocks: [Block] {
        var out: [Block] = []
        var code: [String]?
        var para: [String] = []
        func flushPara() {
            if !para.isEmpty { out.append(.paragraph(para.joined(separator: " "))); para = [] }
        }
        for raw in markdown.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("```") {
                if let c = code {
                    out.append(.code(c.joined(separator: "\n")))
                    code = nil
                } else {
                    flushPara()
                    code = []
                }
                continue
            }
            if code != nil {
                code?.append(raw)
                continue
            }
            if line.isEmpty {
                flushPara()
            } else if line.hasPrefix("#") {
                flushPara()
                let level = line.prefix(while: { $0 == "#" }).count
                out.append(.heading(level, line.drop(while: { $0 == "#" }).trimmingCharacters(in: .whitespaces)))
            } else if line.hasPrefix("- ") || line.hasPrefix("* ") {
                flushPara()
                out.append(.bullet(String(line.dropFirst(2))))
            } else {
                para.append(line)
            }
        }
        if let c = code { out.append(.code(c.joined(separator: "\n"))) }
        flushPara()
        return out
    }

    private func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(s)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .heading(let level, let text):
                    Text(inline(text))
                        .font(.system(size: level <= 1 ? 18 : (level == 2 ? 15.5 : 14), weight: .semibold))
                        .foregroundStyle(Theme.text)
                        .padding(.top, level <= 2 ? 6 : 2)
                case .bullet(let text):
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("•").foregroundStyle(Theme.tertiary)
                        Text(inline(text))
                            .foregroundStyle(Theme.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.system(size: 14))
                case .code(let text):
                    Text(text)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.secondary)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.surface2))
                case .paragraph(let text):
                    Text(inline(text))
                        .font(.system(size: 14))
                        .foregroundStyle(Theme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .tint(.white)
        .textSelection(.enabled)
    }
}

struct ReportCard: View {
    let markdown: String
    var sources: [JSONValue] = []
    var devinURL: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            MarkdownView(markdown: markdown)

            if !sources.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    SectionLabel(text: "Linkup sources")
                    ForEach(Array(sources.enumerated()), id: \.offset) { _, source in
                        let title = source["title"]?.string ?? source["name"]?.string ?? source["url"]?.string ?? source.compact
                        let urlString = source["url"]?.string ?? source.string
                        if let urlString, let url = URL(string: urlString) {
                            Link(destination: url) {
                                sourceRow(title: title, host: url.host ?? urlString)
                            }
                        } else {
                            sourceRow(title: title, host: nil)
                        }
                    }
                }
            }

            if let devinURL, let url = URL(string: devinURL) {
                Link(destination: url) {
                    HStack(spacing: 8) {
                        Image(systemName: "wrench.and.screwdriver.fill").font(.system(size: 12))
                        Text("Open Devin fix session").font(Theme.mono(12.5, .semibold))
                        Spacer()
                        Image(systemName: "arrow.up.right").font(.system(size: 11, weight: .semibold))
                    }
                    .foregroundStyle(.black)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 11)
                    .background(Capsule().fill(Color.white))
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.surface))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke, lineWidth: 0.5))
    }

    private func sourceRow(title: String, host: String?) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "link").font(.system(size: 11)).foregroundStyle(Theme.tertiary).padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13)).foregroundStyle(Theme.text).multilineTextAlignment(.leading)
                if let host {
                    Text(host).font(Theme.mono(10.5)).foregroundStyle(Theme.tertiary)
                }
            }
            Spacer(minLength: 0)
        }
    }
}
