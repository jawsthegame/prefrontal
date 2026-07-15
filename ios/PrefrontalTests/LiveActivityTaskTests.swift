import XCTest

@testable import Prefrontal

/// Unit tests for the "current task" selection that drives the general
/// time-externalization Live Activity (M2) — `Todo.current(in:)`. Pure value
/// logic over decoded `Todo`s (no ActivityKit / network), matching the server's
/// "working on" definition: `started_at` set, status still `open`.
final class LiveActivityTaskTests: XCTestCase {

    /// Decode a `[Todo]` from the server's snake_case JSON shape, so the test
    /// exercises the same Codable path the app uses.
    private func todos(_ json: String) throws -> [Todo] {
        try JSONDecoder().decode(TodoList.self, from: Data(json.utf8)).todos
    }

    func testPicksTheOnlyStartedOpenTodo() throws {
        let list = try todos("""
        {"todos": [
          {"id": 1, "title": "Draft the memo", "status": "open", "started_at": "2026-07-15 09:00:00"},
          {"id": 2, "title": "Unstarted thing", "status": "open"}
        ]}
        """)
        XCTAssertEqual(Todo.current(in: list)?.id, 1)
    }

    func testMostRecentlyStartedWinsWhenSeveralAreInProgress() throws {
        let list = try todos("""
        {"todos": [
          {"id": 1, "title": "Earlier", "status": "open", "started_at": "2026-07-15 08:00:00"},
          {"id": 2, "title": "Latest",  "status": "open", "started_at": "2026-07-15 11:30:00"},
          {"id": 3, "title": "Middle",  "status": "open", "started_at": "2026-07-15 10:00:00"}
        ]}
        """)
        // The one you're actually on right now is the most recent start.
        XCTAssertEqual(Todo.current(in: list)?.id, 2)
    }

    func testIgnoresStartedButClosedTodos() throws {
        // A todo can carry started_at yet be done/dropped — it's not "current".
        let list = try todos("""
        {"todos": [
          {"id": 1, "title": "Finished",  "status": "done",    "started_at": "2026-07-15 09:00:00"},
          {"id": 2, "title": "Abandoned", "status": "dropped", "started_at": "2026-07-15 10:00:00"}
        ]}
        """)
        XCTAssertNil(Todo.current(in: list))
    }

    func testNilWhenNothingIsStarted() throws {
        let list = try todos("""
        {"todos": [
          {"id": 1, "title": "Open but not started", "status": "open"}
        ]}
        """)
        XCTAssertNil(Todo.current(in: list))
    }

    func testEmptyListIsNil() throws {
        XCTAssertNil(Todo.current(in: []))
    }
}
