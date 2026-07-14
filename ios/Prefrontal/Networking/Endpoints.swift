import Foundation

/// Typed calls layered over `APIClient`. Paths + shapes mirror the FastAPI
/// service in prefrontal/webhooks/routers/*.py.
extension APIClient {
    // Reads
    func todos() async throws -> [Todo] { try await get("todos", as: TodoList.self).todos }
    func todosNow(cap: Int = 60) async throws -> TodosNow {
        try await get("todos/now", query: ["cap_minutes": "\(cap)"], as: TodosNow.self)
    }
    func todosFit(minutes: Int) async throws -> TodosFit {
        try await get("todos/fit", query: ["minutes": "\(minutes)"], as: TodosFit.self)
    }
    func commitments(limit: Int = 50) async throws -> [Commitment] {
        try await get("commitments", query: ["limit": "\(limit)"], as: CommitmentList.self).commitments
    }
    /// The full commitments payload — upcoming plus the recently-elapsed
    /// `previous` list that awaits a made/missed answer.
    func commitmentList(limit: Int = 60) async throws -> CommitmentList {
        try await get("commitments", query: ["limit": "\(limit)"], as: CommitmentList.self)
    }
    func slots(minutes: Int = 30) async throws -> Slots {
        try await get("calendar/slots", query: ["minutes": "\(minutes)"], as: Slots.self)
    }
    func departureNext() async throws -> DepartureNext { try await get("departure/next", as: DepartureNext.self) }
    func outings() async throws -> Outings { try await get("outings", as: Outings.self) }
    func focus() async throws -> FocusState { try await get("focus", as: FocusState.self) }
    func nudges(limit: Int = 8) async throws -> [Nudges.Nudge] {
        try await get("nudges", query: ["limit": "\(limit)"], as: Nudges.self).nudges
    }
    func selfCare() async throws -> SelfCare { try await get("self-care", as: SelfCare.self) }
    /// Today's end-of-day self-care gap analysis (timeline gaps + wins). Pure read.
    func selfCareReview() async throws -> SelfCareReview {
        try await get("self-care/review", as: SelfCareReview.self)
    }
    func availableHours() async throws -> AvailableHours {
        try await get("schedule/available-hours", as: AvailableHours.self)
    }
    /// The web-configured location tunables the app applies to `LocationMonitor`.
    func locationSettings() async throws -> LocationSettings {
        try await get("schedule/location-settings", as: LocationSettings.self)
    }
    func briefing() async throws -> Briefing { try await get("briefing", as: Briefing.self) }
    func panic() async throws -> Panic { try await get("panic", as: Panic.self) }
    /// Read-only mail snapshot: messages awaiting action + a recent feed. Safe
    /// to poll (no side effects). Empty lists when mail monitoring is unconfigured.
    func mail() async throws -> MailInbox { try await get("mail", as: MailInbox.self) }
    /// Aggregated behavioral insights (estimates, follow-through, channels, …). Pure read.
    func stats() async throws -> Stats { try await get("stats/data", as: Stats.self) }
    /// Focus-balance rollup — out-of-home time per life-domain over `days`. Pure read.
    func focusBalance(days: Int = 7) async throws -> FocusBalance {
        try await get("balance", query: ["days": "\(days)"], as: FocusBalance.self)
    }

    // Clarifications — hone vague todos/commitments into startable items.
    func clarifications() async throws -> ClarificationList {
        try await get("clarifications", as: ClarificationList.self)
    }
    /// Run the ambiguity sweep now; returns how many new questions were filed.
    @discardableResult
    func runClarificationSweep() async throws -> Int {
        try await post("clarifications/check", as: SweepResult.self).created
    }
    /// Answer a clarification by picking an offered reading (`optionIndex`) or with
    /// free text. Returns the honed reading plus a `playbook` when one applies.
    func resolveClarification(_ id: Int, optionIndex: Int? = nil,
                              answer: String? = nil) async throws -> ClarificationResolveResult {
        var body: [String: Any] = [:]
        if let optionIndex { body["option_index"] = optionIndex }
        if let answer, !answer.isEmpty { body["answer"] = answer }
        return try await post("clarifications/\(id)/resolve", json: body, as: ClarificationResolveResult.self)
    }
    /// Dismiss a clarification ("not ambiguous") — the sweep won't re-ask it.
    func dismissClarification(_ id: Int) async throws {
        try await post("clarifications/\(id)/dismiss")
    }
    /// Re-fetch a task type's guided walkthrough (localized when opted in).
    func playbook(taskType: String) async throws -> Playbook {
        try await get("clarifications/playbooks/\(taskType)", as: Playbook.self)
    }

    // Todo writes
    func addTodo(title: String) async throws { try await post("todos", json: ["title": title], queueable: true) }
    func startTodo(_ id: Int) async throws { try await post("todos/\(id)/start") }
    func unstartTodo(_ id: Int) async throws { try await post("todos/\(id)/unstart") }
    func closeTodo(_ id: Int, done: Bool) async throws { try await post("todos/\(id)/\(done ? "done" : "drop")") }
    func decomposeTodo(_ id: Int) async throws { try await post("todos/\(id)/decompose") }
    func markStepDone(_ id: Int, step: Int) async throws { try await post("todos/\(id)/steps/\(step)/done") }

    // Delegation — hand a todo to the in-app AI agent or email a human VA.
    func delegateRecipients() async throws -> [String] {
        try await get("todos/delegate-recipients", as: Recipients.self).recipients
    }
    func delegateTodo(_ id: Int, handler: String, destination: String? = nil,
                      context: String? = nil, note: String? = nil) async throws {
        var body: [String: Any] = ["handler": handler]
        if let destination, !destination.isEmpty { body["destination"] = destination }
        if let context, !context.isEmpty { body["context"] = context }
        if let note, !note.isEmpty { body["note"] = note }
        try await post("todos/\(id)/delegate", json: body)
    }
    func returnDelegation(_ id: Int) async throws { try await post("todos/\(id)/delegate/return") }

    // Self-care. `reset` wraps a quota check that's at its target back to zero
    // (the mobile tap-at-max cycle — touch has no shift-click to rewind); `undo`
    // rewinds one. `reset` takes precedence over `undo` server-side.
    func markSelfCare(key: String, undo: Bool = false, reset: Bool = false) async throws {
        try await post("self-care/mark",
                       json: ["key": key, "undo": undo, "reset": reset], queueable: true)
    }

    // Available hours — a partial write of one or more weekdays; the server
    // merges over the stored schedule and echoes the fresh seven-day view back.
    @discardableResult
    func setAvailableHours(_ days: [String: AvailableHours.Day]) async throws -> AvailableHours {
        let payload = days.mapValues {
            ["available": $0.available, "start": $0.start, "end": $0.end] as [String: Any]
        }
        return try await post("schedule/available-hours", json: ["days": payload], as: AvailableHours.self)
    }

    // Commitment outcome (honest made/missed self-report on an elapsed event).
    // Pass nil to clear the answer, resurfacing it if still in-window.
    func setCommitmentOutcome(_ id: Int, outcome: String?) async throws {
        try await post("commitments/\(id)/outcome", json: ["outcome": outcome ?? NSNull()])
    }

    // Hide (or un-hide) a commitment. Hiding drops it from every surface that
    // reads upcoming_commitments, and — because previous_commitments also
    // excludes hidden rows — clears it from the "Did you make it?" list, the
    // escape hatch for FYI events you never had to attend.
    func setCommitmentHidden(_ id: Int, hidden: Bool = true) async throws {
        try await post("commitments/\(id)/hidden", json: ["hidden": hidden])
    }

    // Briefing 👍/👎 — steers the LLM briefing voice over time.
    func briefingFeedback(helpful: Bool) async throws {
        try await post("briefing/feedback", json: ["helpful": helpful])
    }

    // Register (or clear, with "") this device's APNs token for native push.
    func registerApnsToken(_ token: String) async throws {
        try await post("route/apns-token", json: ["token": token])
    }

    // Geofencing (#469) — curated places to monitor, and the position/departure
    // pings a region crossing fires.
    func places() async throws -> [Place] { try await get("places", as: PlacesList.self).places }
    func postLocation(lat: Double, lon: Double, accuracy: Double? = nil) async throws {
        var body: [String: Any] = ["lat": lat, "lon": lon]
        if let accuracy { body["accuracy_m"] = accuracy }
        try await post("webhooks/location", json: body)
    }
    func postDepartureLeft(lat: Double? = nil, lon: Double? = nil) async throws {
        var body: [String: Any] = [:]
        if let lat { body["current_lat"] = lat }
        if let lon { body["current_lon"] = lon }
        try await post("webhooks/departure/left", json: body)
    }

    // One-tap outcome log (the /webhooks/shortcut path). `action` is made_it /
    // missed_it / partial; `episodeType` defaults to a departure. `source` tags
    // provenance for the server's usage stats and defaults to `app_intent` —
    // every caller here is a native App Intent, distinct from a hand-built
    // fallback Shortcut (which omits it and the server defaults to `shortcut`).
    func logShortcut(action: String, episodeType: String = "departure",
                     channel: String = "notification",
                     source: String = "app_intent") async throws {
        try await post("webhooks/shortcut",
                       json: ["action": action, "episode_type": episodeType,
                              "channel": channel, "source": source],
                       queueable: true)
    }

    // Focus / outing lifecycle (webhook routes)
    func startFocus(task: String, minutes: Int?) async throws {
        var body: [String: Any] = ["intended_task": task, "aligned": true]
        if let minutes { body["planned_minutes"] = minutes }
        try await post("webhooks/focus/start", json: body)
    }
    func endFocus() async throws { try await post("webhooks/focus/end") }
    func startOuting(intention: String, minutes: Int?) async throws {
        var body: [String: Any] = ["intention": intention]
        if let minutes { body["time_window_minutes"] = minutes }
        try await post("webhooks/outing/start", json: body)
    }
    func returnOuting() async throws { try await post("webhooks/outing/return") }

    // Impulsivity — park an impulse as a todo, and the reflective-pause on the
    // pull to switch off the active focus block (the native replacements for the
    // capture / reflective-pause Shortcuts).
    func captureImpulse(_ text: String, priority: Int = 1) async throws -> ImpulseCaptured {
        try await post("webhooks/impulse/capture",
                       json: ["impulse_text": text, "priority": priority],
                       as: ImpulseCaptured.self)
    }
    func focusSwitch() async throws -> SwitchPause {
        try await post("webhooks/focus/switch", as: SwitchPause.self)
    }

    // Trip retro — close out the newest unlabeled trip in one call (label + note).
    func tripRetro(label: String, reflection: String?) async throws -> TripRetroResult {
        var body: [String: Any] = ["label": label]
        if let reflection, !reflection.isEmpty { body["reflection"] = reflection }
        return try await post("webhooks/trip/retro", json: body, as: TripRetroResult.self)
    }
}

/// Convenience: build a client on the main actor, then run an async call.
@MainActor
func withAPI<T>(_ body: (APIClient) async throws -> T) async throws -> T {
    let client = try APIClient()
    return try await body(client)
}
