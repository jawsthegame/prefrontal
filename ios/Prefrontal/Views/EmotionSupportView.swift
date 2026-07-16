import SwiftUI

/// In-the-moment emotion-regulation support — the *feeling* side of a hard moment,
/// the sibling to Panic (tasks) and the day-shaped encouragement layer. On demand
/// only: the user reaches in with one tap or a few words, and the server returns
/// **one** brief, evidence-matched micro-skill (ACT / DBT distress-tolerance /
/// self-compassion) fitted to the feeling.
///
/// Safety boundary mirrors the server (`prefrontal/emotion_regulation.py`), which
/// screens for crisis language *first*: when `kind == "crisis"` the response is
/// resources and an urge to reach a person — never a coping skill. This view
/// renders whatever the server returns **verbatim** (via `MarkdownText`) and keys
/// its framing off `kind`; it never composes coping content of its own, and never
/// offers "try another" on a crisis response. General-wellness support, not
/// therapy.
struct EmotionSupportView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""
    @State private var support: EmotionSupport?
    @State private var error: String?
    // Qualified: the app has its own `FocusState` *model* struct that would
    // otherwise shadow SwiftUI's property wrapper here.
    @SwiftUI.FocusState private var editorFocused: Bool

    private var trimmed: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let error { ErrorBanner(message: error) }
                    promptCard
                    if let s = support {
                        if s.isCrisis { crisisCard(s) } else { skillCard(s) }
                    }
                }
                .padding(16)
            }
            .brandScreen()
            .navigationTitle("A hard moment")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
        }
    }

    // The ask — describe the feeling, or just reach for help with nothing typed.
    private var promptCard: some View {
        Card {
            CardLabel(text: "How are you feeling?")
            Text("Tell me what's going on — or just take a breath, no words needed. I'll offer one small thing to try.")
                .font(.subheadline).foregroundStyle(Brand.muted)
                .fixedSize(horizontal: false, vertical: true)
            TextField("What's going on? (optional)", text: $draft, axis: .vertical)
                .lineLimit(2...5)
                .foregroundStyle(Brand.nearWhite)
                .focused($editorFocused)
                .padding(10)
                .background(Brand.navyRaised, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.line))
            AsyncButton {
                editorFocused = false
                await fetch()
            } label: {
                Label(trimmed.isEmpty ? "Take a breath" : "Get support", systemImage: "figure.mind.and.body")
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
            }
            .background(Brand.teal.opacity(0.9), in: RoundedRectangle(cornerRadius: 14))
            .foregroundStyle(Brand.accentFg)
        }
    }

    // A coping micro-skill — rendered verbatim; its tradition named as a gentle
    // subtitle, the inferred feeling shown only when it's specific.
    private func skillCard(_ s: EmotionSupport) -> some View {
        Card {
            CardLabel(text: familyLabel(s.family))
            if let sub = stateSubtitle(s.state) {
                Text(sub).font(.footnote).foregroundStyle(Brand.teal)
            }
            MarkdownText(text: s.text)
            AsyncButton {
                await fetch()
            } label: {
                Label("Try another", systemImage: "arrow.triangle.2.circlepath").font(.footnote)
            }
            .foregroundStyle(Brand.muted)
            .padding(.top, 2)
        }
    }

    // A crisis response — resources, never a skill, and no "try another". The
    // one-tap links reach the line the server's message already names.
    private func crisisCard(_ s: EmotionSupport) -> some View {
        Card {
            HStack(spacing: 8) {
                Image(systemName: "heart.circle.fill").foregroundStyle(Brand.danger)
                CardLabel(text: "Please reach out")
            }
            MarkdownText(text: s.text)
            HStack(spacing: 12) {
                crisisLink("Call 988", systemImage: "phone.fill", url: "tel:988")
                crisisLink("Text 988", systemImage: "message.fill", url: "sms:988")
            }
            .padding(.top, 4)
        }
    }

    @ViewBuilder private func crisisLink(_ title: String, systemImage: String, url: String) -> some View {
        if let dest = URL(string: url) {
            Link(destination: dest) {
                Label(title, systemImage: systemImage)
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity).padding(.vertical, 10)
            }
            .background(Brand.danger.opacity(0.9), in: RoundedRectangle(cornerRadius: 12))
            .foregroundStyle(.white)
        }
    }

    private func fetch() async {
        do {
            support = try await withAPI { try await $0.emotionSupport(text: trimmed.isEmpty ? nil : trimmed) }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    private func familyLabel(_ family: String?) -> String {
        switch family {
        case "dbt": return "A distress-tolerance skill"
        case "act": return "An acceptance skill"
        case "self_compassion": return "A self-compassion skill"
        default: return "Something to try"
        }
    }

    // The inferred feeling, shown only when it's a specific one worth naming —
    // "generic" is the catch-all fallback, so don't surface it.
    private func stateSubtitle(_ state: String?) -> String? {
        guard let s = state, !s.isEmpty, s != "generic" else { return nil }
        return "for a moment of \(s)"
    }
}
