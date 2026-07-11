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
    func briefing() async throws -> Briefing { try await get("briefing", as: Briefing.self) }
    func panic() async throws -> Panic { try await get("panic", as: Panic.self) }

    // Todo writes
    func addTodo(title: String) async throws { try await post("todos", json: ["title": title]) }
    func startTodo(_ id: Int) async throws { try await post("todos/\(id)/start") }
    func unstartTodo(_ id: Int) async throws { try await post("todos/\(id)/unstart") }
    func closeTodo(_ id: Int, done: Bool) async throws { try await post("todos/\(id)/\(done ? "done" : "drop")") }
    func decomposeTodo(_ id: Int) async throws { try await post("todos/\(id)/decompose") }
    func markStepDone(_ id: Int, step: Int) async throws { try await post("todos/\(id)/steps/\(step)/done") }

    // Self-care
    func markSelfCare(key: String, undo: Bool = false) async throws {
        try await post("self-care/mark", json: ["key": key, "undo": undo])
    }

    // Commitment outcome (honest made/missed self-report on an elapsed event).
    // Pass nil to clear the answer, resurfacing it if still in-window.
    func setCommitmentOutcome(_ id: Int, outcome: String?) async throws {
        try await post("commitments/\(id)/outcome", json: ["outcome": outcome ?? NSNull()])
    }

    // Briefing 👍/👎 — steers the LLM briefing voice over time.
    func briefingFeedback(helpful: Bool) async throws {
        try await post("briefing/feedback", json: ["helpful": helpful])
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
}

/// Convenience: build a client on the main actor, then run an async call.
@MainActor
func withAPI<T>(_ body: (APIClient) async throws -> T) async throws -> T {
    let client = try APIClient()
    return try await body(client)
}
