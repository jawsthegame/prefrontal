import SwiftUI

/// A button that runs an async action, showing a spinner and surfacing errors.
struct AsyncButton<Label: View>: View {
    var role: ButtonRole? = nil
    let action: () async throws -> Void
    @ViewBuilder var label: Label
    var onError: (String) -> Void = { _ in }

    @State private var running = false

    var body: some View {
        Button(role: role) {
            guard !running else { return }
            running = true
            Task {
                defer { running = false }
                do { try await action() }
                catch { onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription) }
            }
        } label: {
            ZStack {
                label.opacity(running ? 0 : 1)
                if running { ProgressView().controlSize(.small) }
            }
        }
        .disabled(running)
    }
}

/// Inline error strip.
struct ErrorBanner: View {
    let message: String
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Brand.danger)
            Text(message).font(.footnote).foregroundStyle(Brand.fg)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Brand.danger.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.danger.opacity(0.35)))
    }
}

/// Small-caps section heading used across cards.
struct CardLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.bold))
            .tracking(0.6)
            .foregroundStyle(Brand.muted)
    }
}

// MARK: - Pills (mirror the web dashboard's pill system)

/// Neutral chip (default), or a filled semantic chip.
struct Chip: View {
    let text: String
    var color: Color? = nil          // when set → tinted bg + fg
    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background((color ?? Brand.chip).opacity(color == nil ? 1 : 0.16), in: Capsule())
            .foregroundStyle(color ?? Brand.muted)
    }
}

/// Outlined domain/tag pill, colored from a hash of the label.
struct DomainPill: View {
    let text: String
    var body: some View {
        let c = Brand.domain(text)
        Text(text.lowercased())
            .font(.caption2.weight(.bold))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .overlay(Capsule().stroke(c, lineWidth: 1))
            .foregroundStyle(c)
    }
}

/// A small filled level dot (for nudges/departure).
struct LevelDot: View {
    let level: String?
    var body: some View { Circle().fill(Brand.level(level)).frame(width: 7, height: 7) }
}

/// A self-care chip whose background fills left→right toward its target,
/// echoing the web `.sc-chip` progress gradient.
struct ProgressChip: View {
    let icon: String
    let label: String
    let count: Int
    let target: Int
    let satisfied: Bool
    let overdue: Bool

    private var fraction: Double { target <= 0 ? (satisfied ? 1 : 0) : min(1, Double(count) / Double(target)) }

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Brand.chip
                Brand.good.opacity(0.28).frame(width: geo.size.width * fraction)
                HStack(spacing: 6) {
                    Text(icon)
                    Text(label).font(.subheadline.weight(.medium)).foregroundStyle(Brand.fg)
                    Spacer(minLength: 4)
                    Text("\(count)/\(target)")
                        .font(.subheadline.weight(.semibold)).monospacedDigit()
                        .foregroundStyle(satisfied ? Brand.good : (overdue ? Brand.warn : Brand.muted))
                }
                .padding(.horizontal, 12)
            }
        }
        .frame(height: 42)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .stroke(satisfied ? Brand.good : Brand.line, lineWidth: 1))
    }
}

/// Swipe-left-to-reveal a single trailing action, for our card-based layouts
/// where a `List`'s `.swipeActions` isn't available. The row content slides
/// left over a pinned action button; releasing past a short threshold snaps it
/// open (tap the button to confirm), and a full swipe fires `action` outright —
/// the familiar "swipe to delete" idiom. Vertical drags fall through to the
/// enclosing `ScrollView` untouched (the gesture only engages once horizontal
/// movement dominates), so scrolling still works over a swipeable row.
struct SwipeToReveal<Content: View>: View {
    var label: String = "Hide"
    var systemImage: String = "eye.slash.fill"
    var tint: Color = Brand.muted
    /// Card fill the sliding content sits on, so it fully masks the button when
    /// closed. Defaults to the standard card surface.
    var surface: Color = Brand.card
    let action: () async -> Void
    @ViewBuilder var content: Content

    @State private var offset: CGFloat = 0      // live horizontal offset (≤ 0)
    @State private var settled: CGFloat = 0     // resting offset (0 or -revealWidth)
    @State private var engaged = false          // this drag has claimed the horizontal axis

    private let revealWidth: CGFloat = 84
    private let commitThreshold: CGFloat = 180

    var body: some View {
        ZStack(alignment: .trailing) {
            Button { fire() } label: {
                VStack(spacing: 3) {
                    Image(systemName: systemImage).font(.body)
                    Text(label).font(.caption2.weight(.semibold))
                }
                .foregroundStyle(.white)
                .frame(width: revealWidth)
                .frame(maxHeight: .infinity)
                .background(tint)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            }
            .buttonStyle(.plain)
            .opacity(offset < -1 ? 1 : 0)
            // When the row is closed the button is fully masked by the content;
            // drop it from hit-testing and the accessibility tree too, so it
            // can't be tapped through or focused by VoiceOver until revealed.
            .allowsHitTesting(offset < -1)
            .accessibilityHidden(offset >= -1)

            content
                .background(surface)
                .offset(x: offset)
                // Simultaneous (not high-priority) so a vertical drag still
                // scrolls the enclosing ScrollView; we only move the row once
                // the swipe is clearly horizontal (see `engaged`).
                .simultaneousGesture(drag)
        }
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var drag: some Gesture {
        DragGesture(minimumDistance: 12)
            .onChanged { v in
                if !engaged {
                    // Only take over once the swipe is clearly horizontal; a
                    // mostly-vertical drag stays with the ScrollView.
                    guard abs(v.translation.width) > abs(v.translation.height) else { return }
                    engaged = true
                }
                offset = min(0, settled + v.translation.width)
            }
            .onEnded { v in
                // A drag that stayed vertical never engaged, so it left `offset`
                // at rest — leave it there rather than acting on its stray width.
                guard engaged else { return }
                engaged = false
                let dx = settled + v.translation.width
                if dx <= -commitThreshold {
                    withAnimation(.easeOut(duration: 0.18)) { offset = -600 }
                    fire()
                } else if dx <= -revealWidth * 0.5 {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                        offset = -revealWidth; settled = -revealWidth
                    }
                } else {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                        offset = 0; settled = 0
                    }
                }
            }
    }

    private func fire() {
        Task {
            await action()
            // On success the row is removed by the caller's reload and this is a
            // no-op on a gone view; if the action failed the row survives, so
            // settle it back closed instead of leaving it flung off-screen.
            withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                offset = 0; settled = 0
            }
        }
    }
}

/// A simple wrapping row — lays children left→right, wrapping to new lines.
struct FlowRow: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > maxWidth, x > 0 { x = 0; y += lineH + lineSpacing; lineH = 0 }
            x += s.width + spacing
            lineH = max(lineH, s.height)
        }
        return CGSize(width: maxWidth == .infinity ? x : maxWidth, height: y + lineH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) {
        var x = bounds.minX, y = bounds.minY, lineH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > bounds.maxX, x > bounds.minX { x = bounds.minX; y += lineH + lineSpacing; lineH = 0 }
            v.place(at: CGPoint(x: x, y: y), anchor: .topLeading, proposal: ProposedViewSize(s))
            x += s.width + spacing
            lineH = max(lineH, s.height)
        }
    }
}

/// Lightweight Markdown renderer for the server's digests (e.g. the morning
/// briefing, which is `render_briefing` output: `##` section headers, `-`
/// bullets, `**bold**`, blank-line-separated blocks).
///
/// SwiftUI's `Text` only parses Markdown from *string literals*, not a runtime
/// `String`, and `AttributedString(markdown:)` handles only **inline** syntax
/// (bold/italic/links/code) — not block-level headers or lists. So this splits
/// into lines, classifies each block, and renders inline emphasis per line via
/// `AttributedString`. A leading `# …` title is dropped (the card supplies its
/// own heading). `lineLimit` caps the number of rendered content lines (for a
/// collapsed "Show more" preview); `nil` renders all.
struct MarkdownText: View {
    let text: String
    var lineLimit: Int? = nil

    private enum Kind { case h2, h3, body, bullet }
    private struct Block { let content: AttributedString; let kind: Kind }

    var body: some View {
        let blocks = Self.blocks(from: text, limit: lineLimit)
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in
                switch b.kind {
                case .h2:
                    Text(b.content).font(.headline).foregroundStyle(Brand.nearWhite)
                        .padding(.top, 2)
                case .h3:
                    Text(b.content).font(.subheadline.weight(.semibold))
                        .foregroundStyle(Brand.nearWhite)
                case .body:
                    Text(b.content).font(.subheadline).foregroundStyle(Brand.nearWhite)
                        .fixedSize(horizontal: false, vertical: true)
                case .bullet:
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•").font(.subheadline).foregroundStyle(Brand.muted)
                        Text(b.content).font(.subheadline).foregroundStyle(Brand.nearWhite)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Number of rendered content lines (blank lines and a leading `#` title
    /// excluded) — used to decide whether a "Show more" toggle is warranted. A
    /// lightweight scan: it applies the same filtering as `blocks(from:)` but
    /// skips the per-line `AttributedString` Markdown parse, which is wasted work
    /// for a count that runs on every render.
    static func lineCount(_ text: String) -> Int { contentLines(from: text, limit: nil).count }

    private static func inline(_ s: String) -> AttributedString {
        (try? AttributedString(
            markdown: s,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(s)
    }

    /// Classify a trimmed, non-empty line into its block kind and the text after
    /// any marker — no inline parsing, so it's cheap enough for `lineCount`.
    private static func classify(_ line: String) -> (kind: Kind, text: String) {
        if line.hasPrefix("### ") { return (.h3, String(line.dropFirst(4))) }
        if line.hasPrefix("## ") { return (.h2, String(line.dropFirst(3))) }
        if line.hasPrefix("# ") { return (.h2, String(line.dropFirst(2))) }
        if line.hasPrefix("- ") || line.hasPrefix("* ") { return (.bullet, String(line.dropFirst(2))) }
        return (.body, line)
    }

    /// The content lines to render, blank lines and a leading `#` title dropped.
    /// Trims with `.whitespacesAndNewlines` so a trailing `\r` from CRLF server
    /// text doesn't survive into the content or break the prefix checks.
    private static func contentLines(from text: String, limit: Int?) -> [(kind: Kind, text: String)] {
        var out: [(kind: Kind, text: String)] = []
        var sawContent = false
        for raw in text.components(separatedBy: "\n") {
            let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if t.isEmpty { continue }  // blank lines → uniform VStack spacing
            // Drop a leading H1 title; the card already shows "Morning briefing".
            if !sawContent, t.hasPrefix("# ") { continue }
            sawContent = true
            out.append(classify(t))
            if let limit, out.count >= limit { break }
        }
        return out
    }

    private static func blocks(from text: String, limit: Int?) -> [Block] {
        contentLines(from: text, limit: limit).map { Block(content: inline($0.text), kind: $0.kind) }
    }
}

extension View {
    /// Standard scroll screen on the paper background.
    func brandScreen() -> some View {
        self.scrollContentBackground(.hidden)
            .background(Brand.bg.ignoresSafeArea())
    }
}
