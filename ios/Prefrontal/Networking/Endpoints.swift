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
    /// Overlaps among upcoming commitments (firm double-bookings + soft possibles). Pure read.
    func commitmentConflicts() async throws -> ConflictList {
        try await get("commitments/conflicts", as: ConflictList.self)
    }
    /// Stop flagging an overlap (resurfaces if either event moves).
    func dismissConflict(key: String) async throws {
        try await post("commitments/conflicts/dismiss", json: ["key": key])
    }
    /// Draft (or, with `send`, email) a polite reschedule request to the other party.
    /// `send: false` previews the draft; `send: true` requires `to` and, on success,
    /// dismisses the conflict.
    func rescheduleConflict(key: String, to: String? = nil, recipientName: String? = nil,
                            note: String? = nil, send: Bool = false) async throws -> RescheduleResult {
        var body: [String: Any] = ["key": key, "send": send]
        if let to, !to.isEmpty { body["to"] = to }
        if let recipientName, !recipientName.isEmpty { body["recipient_name"] = recipientName }
        if let note, !note.isEmpty { body["note"] = note }
        return try await post("commitments/conflicts/reschedule", json: body, as: RescheduleResult.self)
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
    /// Per-user feature toggles: the deployment-enabled modules + Context packs,
    /// each flagged with whether this user has it on.
    func features() async throws -> FeatureList {
        try await get("settings/features", as: FeatureList.self)
    }
    /// Turn one feature on/off for this user (`kind` is "modules" or "packs").
    /// Returns the fresh list.
    @discardableResult
    func setFeature(kind: String, key: String, enabled: Bool) async throws -> FeatureList {
        try await post("settings/features", json: [kind: [key: enabled]], as: FeatureList.self)
    }
    /// The web-configured location tunables the app applies to `LocationMonitor`.
    func locationSettings() async throws -> LocationSettings {
        try await get("schedule/location-settings", as: LocationSettings.self)
    }
    func briefing() async throws -> Briefing { try await get("briefing", as: Briefing.self) }
    func panic() async throws -> Panic { try await get("panic", as: Panic.self) }
    /// In-the-moment emotion-regulation support (`POST /emotion/support`) — one
    /// brief, evidence-matched micro-skill for a hard moment. Pass `nil`/empty for
    /// a one-tap request; a few words let the server fit the skill to the feeling.
    /// The server screens for crisis language first and, if it trips, returns
    /// resources (`kind == "crisis"`) instead of a skill — see `EmotionSupport`.
    func emotionSupport(text: String? = nil) async throws -> EmotionSupport {
        var json: [String: Any] = [:]
        if let text, !text.isEmpty { json["text"] = text }
        return try await post("emotion/support", json: json, as: EmotionSupport.self)
    }

    // People — the name-mention review queue. Names that ingested items used but
    // that aren't on the roster yet; identify or dismiss each. The roster feeds the
    // behavioral profile and todo prioritization (`prefrontal/people.py`).

    /// Pending name-mentions awaiting review. Pure read, safe to poll.
    func peopleQueue() async throws -> [PersonMention] {
        try await get("people/queue", as: PersonMentionList.self).mentions
    }
    /// Identify a queued mention by creating + categorizing a new roster person
    /// (`person_id` omitted). `relationship` must be one of the server's
    /// `RELATIONSHIPS`; `importance` is the shared 0–3 priority scale.
    func identifyMention(_ id: Int, relationship: String, importance: Int) async throws {
        try await post("people/mentions/\(id)/identify",
                       json: ["relationship": relationship, "importance": importance])
    }
    /// Dismiss a queued mention — not a person, or not worth tracking.
    func dismissMention(_ id: Int) async throws {
        try await post("people/mentions/\(id)/dismiss")
    }

    /// Still-open captured-and-deferred impulses awaiting retro review, plus a
    /// ready-to-speak retro line. Pure read; triage (keep vs drop) reuses the
    /// todo endpoints — a "drop" is `closeTodo(_:done:false)`.
    func parkedImpulses() async throws -> ParkedImpulses {
        try await get("impulses/parked", as: ParkedImpulses.self)
    }
    /// The single honest next thing to do right now (powers the "one next thing"
    /// widget). One action + reason, never the whole list. Pure read, safe to poll.
    func nextThing() async throws -> NextThing { try await get("next", as: NextThing.self) }
    /// Read-only mail snapshot: messages awaiting action + a recent feed. Safe
    /// to poll (no side effects). Empty lists when mail monitoring is unconfigured.
    func mail() async throws -> MailInbox { try await get("mail", as: MailInbox.self) }
    /// On-demand situation tools the enabled Context Packs contribute (empty if none). Pure read.
    func situations() async throws -> [SituationTool] {
        try await get("packs/situations", as: SituationList.self).situations
    }
    /// Run one situation tool against your live data (read-only); returns its result.
    func runSituation(tool: String) async throws -> SituationResult {
        try await post("packs/situations/\(tool)", as: SituationResult.self)
    }
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

    // Todo edits. `deadlineISO` is an offset-aware ISO-8601 string (the server's
    // to_utc uses the embedded offset); nil clears the deadline. `notes` nil/blank clears.
    func setTodoDeadline(_ id: Int, deadlineISO: String?) async throws {
        try await post("todos/\(id)/deadline", json: ["deadline": deadlineISO ?? NSNull()])
    }
    func setTodoNotes(_ id: Int, notes: String?) async throws {
        try await post("todos/\(id)/notes", json: ["notes": notes ?? NSNull()])
    }

    // Honest-prioritization reads (pure): tasks you keep bailing on, and important
    // todos left sitting.
    func stuckTodos() async throws -> [StuckTodo] {
        try await get("todos/stuck", as: StuckList.self).stuck
    }
    func avoidedTodos() async throws -> [AvoidedTodo] {
        try await get("todos/avoided", as: AvoidedList.self).avoided
    }

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

    // Blockers — who's waiting on you (the ball's in your court). Feeds
    // prioritization; mirrors prefrontal/webhooks/routers/blockers.py.
    func blockers(includeResolved: Bool = false) async throws -> [Blocker] {
        try await get(
            "blockers",
            query: includeResolved ? ["include_resolved": "true"] : [:],
            as: BlockerList.self
        ).blockers
    }
    func addBlocker(person: String, what: String, priority: Int = 1) async throws {
        try await post(
            "blockers",
            json: ["person": person, "what": what, "priority": priority],
            queueable: true
        )
    }
    func resolveBlocker(_ id: Int) async throws { try await post("blockers/\(id)/resolve") }
    func reopenBlocker(_ id: Int) async throws { try await post("blockers/\(id)/reopen") }

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

    // Sensor path — feed a free-text thought to the LLM-as-sensor (`POST /observe`).
    // The sensor only *proposes* pending candidate updates for human review; it
    // never writes an authoritative fact on capture, so this is a safe, no-confirm
    // capture surface (the native "capture this thought" backbone). Returns how
    // many candidates it proposed. Queued on a transport failure — off-tailnet a
    // thought is too easy to lose, so the write replays on reconnect (a replay
    // re-proposes at worst, and pending proposals are reviewed before anything
    // applies, so an at-least-once dup is harmless).
    @discardableResult
    func observe(text: String) async throws -> Int {
        do {
            return try await post("observe", json: ["text": text], as: ObserveResult.self).count
        } catch APIError.transport {
            OfflineQueue.enqueue(path: "observe", body: ["text": text])
            return 0
        }
    }

    // Brain-dump (roadmap M1) — one ramble fanned out to both capture paths, and
    // the confirm steps that write it. Nothing here writes on capture: `braindump`
    // returns a *preview* (actions) plus *pending* proposals; the actions land only
    // via `applyAssistantActions`, the proposals only via `acceptProposal`.

    /// Send the raw ramble for the **server** to parse — the escalation path (the
    /// opt-in cloud agent, else the local model, does the reasoning).
    func braindump(text: String) async throws -> BrainDumpResponse {
        try await post("braindump", json: ["text": text], as: BrainDumpResponse.self)
    }

    /// Send a structure already parsed **on-device** (Apple Foundation Models):
    /// the server calls no model, just re-validating the supplied actions and
    /// observations and returning the same preview. `observations` carries any
    /// behavioral-episode candidates the on-device pass surfaced (usually empty);
    /// the server allowlist-checks them and records them **pending**, so a
    /// hallucinated candidate drops rather than acting (see `BrainDumpParser`).
    func braindump(parse: ParsedBrainDump) async throws -> BrainDumpResponse {
        let body: [String: Any] = [
            "parse": [
                "actions": parse.wireActions,
                "observations": parse.wireObservations,
                "reply": parse.reply,
            ] as [String: Any]
        ]
        return try await post("braindump", json: body, as: BrainDumpResponse.self)
    }

    /// Execute previewed brain-dump/assistant actions after the user confirms.
    /// The server re-validates them against the current store before writing.
    @discardableResult
    func applyAssistantActions(_ actions: [[String: Any]]) async throws -> ApplyResult {
        try await post("assistant/apply", json: ["actions": actions], as: ApplyResult.self)
    }

    /// Accept one pending behavioral proposal by id (applies it server-side).
    func acceptProposal(_ id: Int) async throws { try await post("proposals/\(id)/accept") }

    // Trip retro — close out the newest unlabeled trip in one call (label + note).
    func tripRetro(label: String, reflection: String?) async throws -> TripRetroResult {
        var body: [String: Any] = ["label": label]
        if let reflection, !reflection.isEmpty { body["reflection"] = reflection }
        return try await post("webhooks/trip/retro", json: body, as: TripRetroResult.self)
    }

    // Trips list — active + recent + the unlabeled trips awaiting a name, with the
    // label-form vocabularies. Pure read, safe to poll.
    func trips() async throws -> TripsSnapshot { try await get("trips", as: TripsSnapshot.self) }

    /// Close a specific trip's retrospective — label + category + domain +
    /// reflection — in one call (`POST /webhooks/trip/retro`). The reflection, when
    /// present, is classified into an outcome that feeds the learning loop.
    func tripRetro(tripId: Int, label: String?, category: String? = nil,
                   domain: String? = nil, reflection: String? = nil) async throws {
        var body: [String: Any] = ["trip_id": tripId]
        if let label, !label.isEmpty { body["label"] = label }
        if let category, !category.isEmpty { body["category"] = category }
        if let domain, !domain.isEmpty { body["domain"] = domain }
        if let reflection, !reflection.isEmpty { body["reflection"] = reflection }
        try await post("webhooks/trip/retro", json: body)
    }

    /// (Re)file a completed trip into a life-domain without touching its label
    /// (`POST /webhooks/trip/domain`); nil/blank clears it.
    func setTripDomain(_ tripId: Int, domain: String?) async throws {
        try await post("webhooks/trip/domain",
                       json: ["trip_id": tripId, "domain": domain ?? NSNull()])
    }
}

/// Shared household sheet — the co-parent surface. Paths mirror
/// prefrontal/webhooks/routers/household.py; every route is scoped to the caller
/// and guarded to members (a caller in no household gets a 404, which the UI
/// turns into the create/join empty state).
extension APIClient {
    // Reads
    /// The whole shared sheet (roster, facts, chores, shopping, charts,
    /// appointments) plus the co-parent surfaces (members, invites, check-in,
    /// digest, balance). Throws `.http(404, …)` when the caller is in no household.
    func householdSheet() async throws -> HouseholdPayload {
        try await get("household/sheet", as: HouseholdPayload.self)
    }
    /// The shopping list on its own — a light read (no `household_seen_at` stamp,
    /// unlike the full sheet), for the Today glance. 404s for a non-member.
    func shoppingList() async throws -> [ShoppingItem] {
        try await get("household/shopping", as: ShoppingList.self).items
    }
    /// Which chores are done + which are scheduled for a local day (0 today, 1
    /// yesterday). Light and side-effect-free — powers the Today glance's
    /// "chores today" count without building (and marking seen) the whole sheet.
    func choresStatus(daysAgo: Int = 0) async throws -> ChoresStatus {
        try await get("household/chores/done", query: ["days_ago": "\(daysAgo)"], as: ChoresStatus.self)
    }

    // Membership (self-serve, no operator needed)
    /// Create a household and join it — the empty-state "start one" path.
    func createHousehold(name: String) async throws {
        try await post("household/create", json: ["name": name])
    }
    /// Join an existing household by redeeming a co-parent's invite code.
    @discardableResult
    func redeemInvite(code: String) async throws -> RedeemResult {
        try await post("household/invites/redeem", json: ["code": code], as: RedeemResult.self)
    }
    /// Mint a shareable invite code (+ join link) to add a co-parent.
    func createInvite() async throws -> InviteMinted {
        try await post("household/invites", as: InviteMinted.self)
    }

    // Roster
    func addChild(name: String, birthday: String? = nil) async throws {
        var body: [String: Any] = ["name": name]
        if let birthday, !birthday.isEmpty { body["birthday"] = birthday }
        try await post("household/children", json: body)
    }
    func addPet(name: String, species: String? = nil, birthday: String? = nil) async throws {
        var body: [String: Any] = ["name": name]
        if let species, !species.isEmpty { body["species"] = species }
        if let birthday, !birthday.isEmpty { body["birthday"] = birthday }
        try await post("household/pets", json: body)
    }

    // Facts — per-member (or household-wide, childId 0) reference facts. Upsert on
    // (category, item, child); `value` nil blanks the value. Clear deletes the row.
    func setFact(category: String, item: String, value: String?, childId: Int = 0) async throws {
        try await post("household/facts",
                       json: ["category": category, "item": item,
                              "value": value ?? NSNull(), "child_id": childId])
    }
    func clearFact(category: String, item: String, childId: Int = 0) async throws {
        try await post("household/facts/clear",
                       json: ["category": category, "item": item, "child_id": childId])
    }

    /// Relay a free-text update to the caller's co-parent(s) as a push — the
    /// "message my co-parent" channel (the free-text counterpart to the trip
    /// check-in's one-tap statuses). A capture write, so it replays off-tailnet;
    /// a solo household is a server-side no-op.
    func relayUpdate(_ text: String) async throws {
        try await post("household/relay", json: ["message": text], queueable: true)
    }

    // Shopping — add is a capture write (queued off-tailnet); the rest are edits.
    func addShopping(item: String, spec: String? = nil, whereToBuy: String? = nil,
                     childId: Int = 0) async throws {
        var body: [String: Any] = ["item": item, "child_id": childId]
        if let spec, !spec.isEmpty { body["spec"] = spec }
        if let whereToBuy, !whereToBuy.isEmpty { body["where_to_buy"] = whereToBuy }
        try await post("household/shopping", json: body, queueable: true)
    }
    func setShoppingGot(_ id: Int, got: Bool) async throws {
        try await post("household/shopping/\(id)/got", json: ["got": got])
    }
    func removeShopping(_ id: Int) async throws { try await post("household/shopping/\(id)/remove") }
    /// Sweep every checked-off item after a shop; still-needed rows stay.
    @discardableResult
    func clearGotShopping() async throws -> ClearResult {
        try await post("household/shopping/clear-got", as: ClearResult.self)
    }

    // Chores — one-tap done/undone. `daysAgo` back-fills yesterday (0 or 1). The
    // done tap is a capture write, so it replays if logged off-tailnet.
    @discardableResult
    func markChoreDone(_ id: Int, daysAgo: Int = 0) async throws -> ChoreDoneResult {
        try await post("household/chores/\(id)/done", json: ["days_ago": daysAgo], as: ChoreDoneResult.self)
    }
    func unmarkChoreDone(_ id: Int, daysAgo: Int = 0) async throws {
        try await post("household/chores/\(id)/undone", json: ["days_ago": daysAgo])
    }

    // Chore setup — upsert (keyed on title within the household), pause/resume,
    // and delete. `ownerId` nil = either parent; `routineId` nil = stands alone;
    // `days` empty = inherit routine / every day; `dueTime` "" = untimed checklist.
    func setChore(title: String, ownerId: Int? = nil, routineId: Int? = nil,
                  days: [Int] = [], dueTime: String = "", impact: String? = nil,
                  enabled: Bool = true) async throws {
        var body: [String: Any] = [
            "title": title, "days": days, "due_time": dueTime, "enabled": enabled,
        ]
        if let ownerId { body["owner_id"] = ownerId }
        if let routineId { body["routine_id"] = routineId }
        if let impact, !impact.isEmpty { body["impact"] = impact }
        try await post("household/chores", json: body)
    }
    func setChoreEnabled(_ id: Int, enabled: Bool) async throws {
        try await post("household/chores/\(id)/enabled", json: ["enabled": enabled])
    }
    func removeChore(_ id: Int) async throws { try await post("household/chores/\(id)/remove") }

    // Star charts — record earned stars; the server congratulates both parents
    // and returns the crossed goals + running total.
    @discardableResult
    func awardStars(_ agreementId: Int, delta: Int = 1, note: String? = nil) async throws -> StarAwardResult {
        var body: [String: Any] = ["delta": delta]
        if let note, !note.isEmpty { body["note"] = note }
        return try await post("household/agreements/\(agreementId)/stars", json: body, as: StarAwardResult.self)
    }

    // Co-parent settings (shared households). The weekly mental-load check-in
    // schedule, and the opt-in daily digest / load-balance toggles.
    func setCheckin(enabled: Bool, day: Int? = nil, time: String? = nil) async throws {
        var body: [String: Any] = ["enabled": enabled]
        // The server rejects enabling without both; a disabled config may omit them.
        body["day"] = day ?? NSNull()
        if let time, !time.isEmpty {
            body["time"] = time
        } else {
            body["time"] = NSNull()
        }
        try await post("household/checkin", json: body)
    }
    func setDigest(enabled: Bool) async throws {
        try await post("household/digest", json: ["enabled": enabled])
    }
    func setBalance(enabled: Bool) async throws {
        try await post("household/balance", json: ["enabled": enabled])
    }

    // Star charts / agreements — create a plan, set its reward tiers (which makes
    // it a chart), set the recurring award-prompt schedule, or remove it.
    func createAgreement(title: String, kind: String = "reward",
                         childId: Int = 0, body: String? = nil) async throws -> AgreementCreated {
        var json: [String: Any] = ["title": title, "kind": kind, "child_id": childId]
        if let body, !body.isEmpty { json["body"] = body }
        return try await post("household/agreements", json: json, as: AgreementCreated.self)
    }
    /// Set/replace the reward tiers from a `"7=small toy, 30=big"` spec (turns a
    /// plain plan into a star chart; the server rejects an empty spec).
    func setStarTiers(_ agreementId: Int, tiers: String) async throws {
        try await post("household/agreements/\(agreementId)/tiers", json: ["tiers": tiers])
    }
    /// Set the recurring "did <kid> earn a star today?" prompt schedule. The server
    /// requires a valid `time` and (when enabled) at least one weekday.
    func setStarPrompt(_ agreementId: Int, enabled: Bool, days: [Int], time: String,
                       question: String? = nil) async throws {
        var json: [String: Any] = ["enabled": enabled, "days": days, "time": time]
        if let question, !question.isEmpty { json["question"] = question }
        try await post("household/agreements/\(agreementId)/prompt", json: json)
    }
    func removeAgreement(_ agreementId: Int) async throws {
        try await post("household/agreements/\(agreementId)/remove")
    }

    // Appointments — a kid appointment as a `kind='child'` commitment. `startAtISO`
    // is an offset-aware ISO-8601 string (the server's to_utc reads the offset).
    func addAppointment(title: String, startAtISO: String, endAtISO: String? = nil,
                        location: String? = nil) async throws {
        var body: [String: Any] = ["title": title, "start_at": startAtISO]
        if let endAtISO, !endAtISO.isEmpty { body["end_at"] = endAtISO }
        if let location, !location.isEmpty { body["location"] = location }
        try await post("household/appointments", json: body)
    }
}

/// Convenience: build a client on the main actor, then run an async call.
@MainActor
func withAPI<T>(_ body: (APIClient) async throws -> T) async throws -> T {
    let client = try APIClient()
    return try await body(client)
}
