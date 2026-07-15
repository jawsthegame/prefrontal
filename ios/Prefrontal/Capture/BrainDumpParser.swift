import Foundation
import FoundationModels

/// On-device brain-dump parsing via **Apple Foundation Models**.
///
/// A rambling voice/text dump is the lowest-friction capture there is, but it
/// mixes several actionable items in one breath ("call the dentist, book the
/// flights, we're out of milk"). The server's `POST /braindump` already turns a
/// ramble into a previewable action list — but doing that parse in the cloud
/// means the raw thought leaves the device and costs a model call. Foundation
/// Models runs a ~3B model *on the device*, so the parse is **private and cheap**:
/// only the resulting structure is sent, to the same endpoint, which re-validates
/// it and returns the preview (see `APIClient.braindump(parse:)`).
///
/// The app's deployment target is iOS 26, so Foundation Models is a hard
/// dependency rather than an availability-gated one — `import FoundationModels` is
/// unconditional. But the *model* can still be unavailable at runtime (Apple
/// Intelligence off, an unsupported device, the model still downloading), so
/// `isAvailable` checks that and `parse` returns `nil` when it can't run — the
/// caller then escalates to the server's own text parse (the opt-in cloud agent
/// for the hard reasoning). A graceful, additive fallback, never a hard failure.
///
/// **Scope.** The on-device pass handles the high-value *actionable* items — the
/// todos, commitments, shopping, and blockers that make up the bulk of a capture,
/// each with a straightforward, reliably-valid wire mapping. The subtler cases —
/// behavioral asides ("I keep blowing off admin on Mondays") and if-then plans
/// (which need a machine-detectable cue) — are left to the server escalation path,
/// which the UI keeps one tap away. Nothing here is authoritative: the server
/// re-validates every action and the user still confirms before any write, so a
/// hallucinated on-device action drops rather than acting.
///
/// (`ParsedBrainDump`, the framework-free result carrier, lives in
/// `Models/BrainDump.swift` so `Endpoints.swift` — compiled into the widget
/// extension too — can reference it without importing this Foundation-Models file.)
enum BrainDumpParser {
    /// Whether an on-device parse can run right now — i.e. the system model is
    /// actually available (not off, unsupported, or still downloading). The UI
    /// reads this to label the capture ("parsed on your device") and the caller to
    /// decide between the on-device and server paths.
    static var isAvailable: Bool {
        // `.unavailable` carries a reason, so match the case rather than assume
        // `Availability` is Equatable.
        if case .available = SystemLanguageModel.default.availability { return true }
        return false
    }

    /// Parse a ramble into structured, server-ready capture actions on-device.
    ///
    /// Returns `nil` when the on-device model is unavailable or errors, so the
    /// caller falls back to sending the raw text for the server to parse. A
    /// non-nil result is a `ParsedBrainDump` whose `wireActions` are ready to POST
    /// as the `parse` body — but they are still only a proposal until the server
    /// validates them and the user applies.
    static func parse(_ text: String) async -> ParsedBrainDump? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, isAvailable else { return nil }
        let session = LanguageModelSession(instructions: Self.instructions)
        do {
            let response = try await session.respond(to: trimmed, generating: Extraction.self)
            return response.content.toParsedBrainDump()
        } catch {
            // Any generation/guardrail error → nil so the caller escalates to the
            // server parse rather than losing the capture.
            return nil
        }
    }

    /// The instruction preamble — mirrors the server assistant's framing so the
    /// two parses stay consistent (capture, don't converse; nothing is done yet).
    private static let instructions = """
        You extract concrete capture items from a rambling brain-dump by an adult \
        with ADHD. Pull out todos, calendar commitments, shopping-list items, and \
        blockers (someone else waiting on the user). Keep each item terse and in \
        the user's own words; do not invent items that weren't said. Times are the \
        user's local wall-clock. The items are only a proposal the user will \
        review — never claim anything is already done.
        """

    // MARK: Guided-generation schema

    /// Uses **guided generation** (`@Generable`): the model is constrained to emit
    /// an `Extraction`, so we get typed fields back instead of parsing free JSON.
    /// Each typed item then maps to the server's wire op; anything the schema can't
    /// express simply isn't captured on-device — the user escalates for those.
    @Generable
    struct Extraction {
        @Guide(description: "A one-sentence, first-person acknowledgement of what will be captured.")
        var reply: String
        @Guide(description: "Standalone tasks the user needs to do.")
        var todos: [Todo]
        @Guide(description: "Scheduled events with a specific date and time.")
        var commitments: [Commitment]
        @Guide(description: "Things to buy.")
        var shopping: [ShoppingItem]
        @Guide(description: "People waiting on the user for something (the ball is in the user's court).")
        var blockers: [Blocker]
    }

    @Generable
    struct Todo {
        @Guide(description: "The task, in the user's words.")
        var title: String
        @Guide(description: "Priority 0=someday, 1=normal, 2=high, 3=urgent.", .range(0...3))
        var priority: Int
        @Guide(description: "Rough time estimate in minutes, or 0 if unstated.")
        var estimateMinutes: Int
        @Guide(description: "Due date as YYYY-MM-DD, or empty if none was stated.")
        var deadline: String
    }

    @Generable
    struct Commitment {
        @Guide(description: "What the event is.")
        var title: String
        @Guide(description: "Local start time as 'YYYY-MM-DD HH:MM'.")
        var startAt: String
        @Guide(description: "Where it is, or empty.")
        var location: String
    }

    @Generable
    struct ShoppingItem {
        @Guide(description: "The item to buy.")
        var item: String
        @Guide(description: "Size/brand/quantity, or empty.")
        var spec: String
    }

    @Generable
    struct Blocker {
        @Guide(description: "Who is waiting on the user.")
        var person: String
        @Guide(description: "What they need from the user.")
        var what: String
    }
}

private extension BrainDumpParser.Extraction {
    /// Map the typed extraction to server wire actions, dropping empties and
    /// normalizing optional fields exactly as the server assistant expects.
    func toParsedBrainDump() -> ParsedBrainDump {
        var actions: [[String: Any]] = []

        for t in todos {
            let title = t.title.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else { continue }
            var a: [String: Any] = ["op": "add_todo", "title": title]
            if (0...3).contains(t.priority), t.priority != 1 { a["priority"] = t.priority }
            if t.estimateMinutes > 0 { a["estimate_minutes"] = t.estimateMinutes }
            let deadline = t.deadline.trimmingCharacters(in: .whitespacesAndNewlines)
            if !deadline.isEmpty { a["deadline"] = deadline }
            actions.append(a)
        }
        for c in commitments {
            let title = c.title.trimmingCharacters(in: .whitespacesAndNewlines)
            let start = c.startAt.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty, !start.isEmpty else { continue }
            var a: [String: Any] = ["op": "add_commitment", "title": title, "start_at": start]
            let loc = c.location.trimmingCharacters(in: .whitespacesAndNewlines)
            if !loc.isEmpty { a["location"] = loc }
            actions.append(a)
        }
        for s in shopping {
            let item = s.item.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !item.isEmpty else { continue }
            var a: [String: Any] = ["op": "add_shopping", "item": item]
            let spec = s.spec.trimmingCharacters(in: .whitespacesAndNewlines)
            if !spec.isEmpty { a["spec"] = spec }
            actions.append(a)
        }
        for b in blockers {
            let person = b.person.trimmingCharacters(in: .whitespacesAndNewlines)
            let what = b.what.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !person.isEmpty, !what.isEmpty else { continue }
            actions.append(["op": "add_blocker", "person": person, "what": what])
        }

        return ParsedBrainDump(
            reply: reply.trimmingCharacters(in: .whitespacesAndNewlines), wireActions: actions)
    }
}
