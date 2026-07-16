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
/// todos, commitments, shopping, and blockers that make up the bulk of a capture —
/// plus the two cases that used to escalate: **if-then plans** ("when I get home,
/// I'll take my meds"), emitted as `add_if_then` actions once a machine-detectable
/// cue (a place, a time window, or arriving/leaving home) is present, and
/// **behavioral episodes** ("I blew off admin again today"), emitted as pending
/// sensor `observations`. The one thing still left to the server escalation path is
/// a *settings change* (a `state` candidate like "stop nudging me after 9") — a
/// hallucinated one would propose editing the user's own preferences, a sharper
/// edge than logging an episode, so it stays behind the deliberate cloud pass the
/// UI keeps one tap away. Nothing here is authoritative regardless: the server
/// re-validates every action against the live store and allowlist-checks every
/// observation, and the user still confirms before any write, so a hallucinated
/// on-device item drops rather than acting.
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
        blockers (someone else waiting on the user). Also pull out two subtler \
        kinds when the user clearly states them: if-then plans ("when/after X, \
        I'll Y") that pair a cue with a tiny action, and behavioral observations \
        (an aside about how something went, e.g. "I blew off admin again"). Keep \
        each item terse and in the user's own words; do not invent items that \
        weren't said. Times are the user's local wall-clock. The items are only a \
        proposal the user will review — never claim anything is already done.
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
        @Guide(description: "If-then plans: a cue the user will run into paired with a tiny action they'll take.")
        var ifThenPlans: [IfThenPlan]
        @Guide(description: "Behavioral observations: brief asides about how something went, for later review.")
        var observations: [Observation]
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

    /// An implementation-intention plan. The server needs a *machine-detectable*
    /// cue to ever fire the plan, so it requires at least one of `place`,
    /// `timeWindow`, or `event`; a plan with none is dropped in the mapping below.
    @Generable
    struct IfThenPlan {
        @Guide(description: "The cue in the user's words — 'when I sit down at my desk', 'after dinner'.")
        var cue: String
        @Guide(description: "The tiny, pre-committed action — 'take my meds', 'text mom back'.")
        var action: String
        @Guide(description: "A place the cue happens at (e.g. 'the office', 'home'), or empty if the cue isn't a place.")
        var place: String
        @Guide(description: "A time-of-day band as 'HH:MM-HH:MM' if the cue is a time window, else empty.")
        var timeWindow: String
        @Guide(
            description: "'arrive_home' or 'leave_home' if the cue is arriving at or leaving home, else empty.",
            .anyOf(["", "arrive_home", "leave_home"]))
        var event: String
    }

    /// A behavioral episode the sensor may record as a pending candidate. Maps to
    /// the server's `{kind:"episode", …}` observation shape; `episodeType` and
    /// `outcome` are constrained to the server's allowlists so the item survives
    /// validation instead of silently dropping.
    @Generable
    struct Observation {
        @Guide(
            description: "The kind of behavioral event.",
            .anyOf(["task", "checkin", "reminder", "departure"]))
        var episodeType: String
        @Guide(
            description: "How it went, or empty if unclear.",
            .anyOf(["", "success", "partial", "miss"]))
        var outcome: String
        @Guide(description: "A short label for what it was about (the task or place), or empty.")
        var context: String
        @Guide(description: "A short note in the user's own words about the behavior.")
        var notes: String
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
        for p in ifThenPlans {
            let cue = p.cue.trimmingCharacters(in: .whitespacesAndNewlines)
            let action = p.action.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !cue.isEmpty, !action.isEmpty else { continue }
            var a: [String: Any] = ["op": "add_if_then", "cue_text": cue, "action_text": action]
            let place = p.place.trimmingCharacters(in: .whitespacesAndNewlines)
            let event = p.event.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if !place.isEmpty { a["place"] = place }
            // Only a window the server's parse_window would accept counts as a cue —
            // else "after dinner" would pass the guard below yet be dropped server-side.
            if let window = normalizedTimeWindow(p.timeWindow) { a["time_window"] = window }
            if event == "arrive_home" || event == "leave_home" { a["event"] = event }
            // The server rejects a plan with no detectable cue; drop it here rather
            // than post one that can only come back as a dropped-item error.
            guard a["place"] != nil || a["time_window"] != nil || a["event"] != nil else { continue }
            actions.append(a)
        }

        var wireObservations: [[String: Any]] = []
        for o in observations {
            let type = o.episodeType.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            guard ["task", "checkin", "reminder", "departure"].contains(type) else { continue }
            let notes = o.notes.trimmingCharacters(in: .whitespacesAndNewlines)
            let context = o.context.trimmingCharacters(in: .whitespacesAndNewlines)
            // A bare episode type with nothing said about it isn't worth a pending
            // proposal — require at least a note or a context label.
            guard !notes.isEmpty || !context.isEmpty else { continue }
            var obs: [String: Any] = [
                "kind": "episode",
                "episode_type": type,
                // The sensor's rationale ideally quotes the source; the aside is it.
                "rationale": notes.isEmpty ? context : notes,
            ]
            let outcome = o.outcome.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if ["success", "partial", "miss"].contains(outcome) { obs["outcome"] = outcome }
            if !context.isEmpty { obs["context"] = context }
            if !notes.isEmpty { obs["notes"] = notes }
            wireObservations.append(obs)
        }

        return ParsedBrainDump(
            reply: reply.trimmingCharacters(in: .whitespacesAndNewlines),
            wireActions: actions,
            wireObservations: wireObservations)
    }
}

/// A `"HH:MM-HH:MM"` window trimmed and echoed back only if the server's
/// `parse_window` would accept it — two `H:MM`/`HH:MM` endpoints with in-range
/// clock values (00:00–23:59) that aren't equal (a `start > end` band is a legal
/// midnight wrap). Returns `nil` for anything else ("after dinner", "9pm", a bad
/// clock), so a bogus window never counts as an if-then cue on the client.
/// Internal (not private) so `BrainDumpTests` can exercise it without the
/// on-device model, which the simulator test host can't run.
func normalizedTimeWindow(_ raw: String) -> String? {
    let spec = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    let parts = spec.split(separator: "-", omittingEmptySubsequences: false)
    guard parts.count == 2 else { return nil }
    func minutes(_ part: Substring) -> Int? {
        let hhmm = part.trimmingCharacters(in: .whitespaces).split(
            separator: ":", omittingEmptySubsequences: false)
        guard hhmm.count == 2 else { return nil }
        let h = hhmm[0], m = hhmm[1]
        guard (1...2).contains(h.count), m.count == 2,
            h.allSatisfy(\.isASCII), h.allSatisfy(\.isNumber),
            m.allSatisfy(\.isASCII), m.allSatisfy(\.isNumber),
            let hours = Int(h), let mins = Int(m),
            (0...23).contains(hours), (0...59).contains(mins)
        else { return nil }
        return hours * 60 + mins
    }
    guard let start = minutes(parts[0]), let end = minutes(parts[1]), start != end else {
        return nil
    }
    return spec
}
