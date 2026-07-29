import Foundation

// Codable models for a delegated todo — mirrors `get_delegation`
// (prefrontal/delegation.py) and the web dashboard's delegation panel. A todo can
// be handed to the in-app AI agent (`agent`), to that agent with tools and the
// freedom to ask you things (`auto`, roadmap M4 — see
// docs/design/auto-mode-delegation.md), or to a human VA over email (`email`).
// We decode a lean subset; unknown keys are ignored.

/// A todo handed to an assistant: the in-app AI agent (writes a brief + drafts +
/// action items back onto the todo) or a human VA over email. Mirrors the
/// server's `get_delegation` shape and the web dashboard's delegation panel.
struct Delegation: Codable, Hashable {
    let handler: String?          // "agent" | "auto" | "email"
    let destination: String?      // VA email (email handler)
    let status: String            // in_prep | prepped | needs_input | forwarded | returned | failed
    let brief: String?
    let detail: String?
    let context: String?
    let drafts: [Draft]?
    let actions: [Action]?
    /// An `auto` run's executed tool calls — the inspectable trail of what it did.
    let steps: [Step]?
    /// What an `auto` run needs from you (`answer` nil until you reply). Non-empty
    /// when `status == "needs_input"`; POST them back to
    /// `/todos/{id}/delegate/answers` (positionally) and the run picks up again.
    let questions: [Question]?

    struct Draft: Codable, Hashable {
        let channel: String?; let to: String?; let subject: String?; let body: String?
    }
    struct Action: Codable, Hashable {
        let text: String?; let mine: Bool?
    }
    struct Step: Codable, Hashable {
        let index: Int?; let server: String?; let tool: String?
        let ok: Bool?; let detail: String?; let observation: String?; let why: String?
    }
    struct Question: Codable, Hashable {
        let text: String?; let why: String?; let answer: String?
    }

    /// (label, done-ish) for the status pill.
    var label: String {
        switch status {
        case "prepped":     return "🤖 Prepped"
        case "forwarded":   return "✉ Sent"
        case "in_prep":     return "… Prepping"
        case "needs_input": return "❓ Needs you"
        case "returned":    return "↩ Returned"
        case "failed":      return "⚠ Needs a hand"
        default:            return status
        }
    }
    var isWorking: Bool { status == "in_prep" }
    var canReturn: Bool { status == "prepped" || status == "forwarded" }
    /// Questions still waiting on an answer — the user's move.
    var pendingQuestions: [Question] {
        (questions ?? []).filter { ($0.answer ?? "").isEmpty }
    }
}
