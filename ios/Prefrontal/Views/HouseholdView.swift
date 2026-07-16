import SwiftUI

/// The **Household** tab — the shared co-parent sheet on the phone. Mirrors the
/// web `/kids` dashboard's daily-driver surface: today's chores (one-tap Done),
/// the shared shopping list, star charts, upcoming kid appointments, the roster
/// with reference facts, the load-balance view, and a catch-up feed. Reads
/// `GET /household/sheet`; a caller in no household gets a 404, which becomes the
/// create/join empty state rather than an error.
struct HouseholdView: View {
    @State private var payload: HouseholdPayload?
    @State private var error: String?
    @State private var loaded = false
    /// True once a 404 tells us the caller belongs to no household — show the
    /// create/join empty state instead of the sheet.
    @State private var noHousehold = false
    @State private var showAllChores = false
    @State private var celebration: String?
    @State private var add: HouseholdAdd?

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                if let error { ErrorBanner(message: error) }
                if let celebration { celebrationBanner(celebration) }
                if noHousehold {
                    HouseholdEmptyState(reload: reloadAfterJoin)
                } else if let p = payload {
                    header(p)
                    ChoresCard(sheet: p.sheet, members: p.members, showAll: $showAllChores,
                               reload: load, onDone: handleChoreDone, onError: { error = $0 })
                    ShoppingCard(items: p.sheet.shopping, reload: load, onError: { error = $0 })
                    ChartsCard(agreements: p.sheet.agreements, reload: load,
                               onAward: { add = .award($0) }, onError: { error = $0 })
                    AppointmentsCard(appointments: p.sheet.upcoming, onAdd: { add = .appointment })
                    RosterCard(sheet: p.sheet, onAddChild: { add = .child }, onAddPet: { add = .pet })
                    if let balance = p.balance, balance.enabled, let view = balance.view {
                        BalanceCard(balance: balance, view: view)
                    }
                    RecentlyChangedCard(changes: p.sheet.recentlyChanged, digest: p.digest)
                } else if !loaded {
                    ProgressView().padding(.top, 40)
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Household")
        .toolbar {
            if payload != nil, !noHousehold {
                ToolbarItem(placement: .topBarTrailing) { addMenu }
            }
        }
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(item: $add, onDismiss: { Task { await load() } }) { sheet in
            switch sheet {
            case .child:       AddRosterSheet(kind: .child)
            case .pet:         AddRosterSheet(kind: .pet)
            case .appointment: AddAppointmentSheet()
            case .invite:      InviteSheet()
            case let .award(agreement): AwardStarsSheet(agreement: agreement)
            }
        }
    }

    private var addMenu: some View {
        Menu {
            Button { add = .appointment } label: { Label("Add appointment", systemImage: "calendar.badge.plus") }
            Button { add = .child } label: { Label("Add child", systemImage: "figure.child") }
            Button { add = .pet } label: { Label("Add pet", systemImage: "pawprint") }
            Divider()
            Button { add = .invite } label: { Label("Invite a co-parent", systemImage: "person.badge.plus") }
        } label: {
            Image(systemName: "plus")
        }
    }

    // MARK: header

    private func header(_ p: HouseholdPayload) -> some View {
        Card {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(p.sheet.householdName ?? "Household")
                        .font(.title3.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                    Text(summary(p.sheet.counts)).font(.caption).foregroundStyle(Brand.muted)
                }
                Spacer(minLength: 8)
                Image(systemName: "house.fill").foregroundStyle(Brand.accent).font(.title3)
            }
            if !p.shared {
                Button { add = .invite } label: {
                    Label("Invite a co-parent", systemImage: "person.badge.plus")
                        .font(.footnote.weight(.medium))
                }
                .buttonStyle(.bordered).tint(Brand.accent)
            }
        }
    }

    private func summary(_ c: HouseholdCounts?) -> String {
        guard let c else { return "Shared with your household" }
        var bits: [String] = []
        func add(_ n: Int?, _ one: String, _ many: String) {
            guard let n, n > 0 else { return }
            bits.append("\(n) \(n == 1 ? one : many)")
        }
        add(c.children, "kid", "kids")
        add(c.pets, "pet", "pets")
        add(c.chores, "chore", "chores")
        add(c.shopping, "thing to buy", "things to buy")
        return bits.isEmpty ? "Shared with your household" : bits.joined(separator: " · ")
    }

    private func celebrationBanner(_ text: String) -> some View {
        HStack(spacing: 8) {
            Text("🎉")
            Text(text).font(.footnote.weight(.medium)).foregroundStyle(Brand.fg)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Brand.good.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.good.opacity(0.35)))
    }

    // MARK: actions

    /// Show a brief celebration when a Done tap finishes a whole routine.
    private func handleChoreDone(_ result: ChoreDoneResult) {
        guard let done = result.routineCompleted, let title = done.title else { return }
        celebration = "\(title) — all done for today!"
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            withAnimation { if celebration == "\(title) — all done for today!" { celebration = nil } }
        }
    }

    /// After joining/creating a household from the empty state, drop the flag and
    /// reload the (now-populated) sheet.
    private func reloadAfterJoin() async {
        noHousehold = false
        loaded = false
        await load()
    }

    private func load() async {
        do {
            let p = try await withAPI { try await $0.householdSheet() }
            payload = p
            noHousehold = false
            error = nil
        } catch APIError.http(404, _) {
            // Not in a household yet — offer to create or join one.
            noHousehold = true
            payload = nil
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

/// Which add/edit sheet is presented over the Household tab.
enum HouseholdAdd: Identifiable {
    case child, pet, appointment, invite
    case award(Agreement)

    var id: String {
        switch self {
        case .child: return "child"
        case .pet: return "pet"
        case .appointment: return "appointment"
        case .invite: return "invite"
        case let .award(a): return "award-\(a.id)"
        }
    }
}

// MARK: - Chores

/// Today's shared chores with one-tap Done, plus setup from the phone. Defaults
/// to today's scheduled, enabled chores; a toggle reveals the full list
/// (including paused ones). The circle tap logs (or clears) today's completion,
/// and finishing a routine's last chore surfaces a celebration via `onDone`. The
/// ＋ adds a chore; a long-press on a row edits it, pauses/resumes, or removes it.
struct ChoresCard: View {
    let sheet: HouseholdSheet
    let members: [HouseholdMember]
    @Binding var showAll: Bool
    let reload: () async -> Void
    let onDone: (ChoreDoneResult) -> Void
    let onError: (String) -> Void
    @State private var editor: ChoreEditor?

    /// Chores not in the default (today's scheduled + enabled) view — paused ones
    /// or those scheduled for another day. Drives whether "Show all" is offered.
    private var hasMore: Bool {
        sheet.chores.contains { !($0.isEnabled && $0.scheduledToday) }
    }
    private var visible: [Chore] {
        showAll ? sheet.chores : sheet.chores.filter { $0.isEnabled && $0.scheduledToday }
    }

    var body: some View {
        Card {
            HStack(spacing: 10) {
                CardLabel(text: showAll ? "All chores" : "Today's chores")
                Spacer()
                if hasMore {
                    Button(showAll ? "Today only" : "Show all") { withAnimation { showAll.toggle() } }
                        .font(.caption.weight(.medium)).tint(Brand.accent)
                }
                Button { editor = .add } label: { Image(systemName: "plus").font(.footnote.weight(.semibold)) }
                    .tint(Brand.accent)
                    .accessibilityLabel("Add a chore")
            }
            if visible.isEmpty {
                Text(showAll ? "No chores set up yet — add one with ＋." : "Nothing due today. 🎉")
                    .font(.footnote).foregroundStyle(Brand.muted)
            } else {
                ForEach(visible) { chore in
                    ChoreRow(chore: chore, reload: reload, onDone: onDone,
                             onEdit: { editor = .edit(chore) }, onError: onError)
                    if chore.id != visible.last?.id { Divider().overlay(Brand.line) }
                }
            }
        }
        .sheet(item: $editor, onDismiss: { Task { await reload() } }) { mode in
            ChoreEditorSheet(mode: mode, members: members, routines: sheet.routines)
        }
    }
}

/// Which chore-editor sheet is up: a fresh add, or an edit of an existing chore.
enum ChoreEditor: Identifiable {
    case add
    case edit(Chore)
    var id: Int { if case let .edit(c) = self { return c.id } else { return 0 } }
}

struct ChoreRow: View {
    let chore: Chore
    let reload: () async -> Void
    let onDone: (ChoreDoneResult) -> Void
    let onEdit: () -> Void
    let onError: (String) -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            AsyncButton {
                if chore.doneToday {
                    try await withAPI { try await $0.unmarkChoreDone(chore.id) }
                } else {
                    let result = try await withAPI { try await $0.markChoreDone(chore.id) }
                    onDone(result)
                }
                await reload()
            } label: {
                Image(systemName: chore.doneToday ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundStyle(chore.doneToday ? Brand.good : Brand.muted)
            } onError: { onError($0) }
            .buttonStyle(.plain)

            VStack(alignment: .leading, spacing: 4) {
                Text(chore.title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(chore.doneToday || !chore.isEnabled ? Brand.muted : Brand.nearWhite)
                    .strikethrough(chore.doneToday, color: Brand.muted)
                FlowRow(spacing: 6) {
                    if !chore.isEnabled { Chip(text: "paused", color: Brand.warn) }
                    if let owner = chore.ownerName, !owner.isEmpty { Chip(text: owner, color: Brand.fyi) }
                    else { Chip(text: "either parent") }
                    if let routine = chore.routineTitle, !routine.isEmpty { DomainPill(text: routine) }
                    let schedule = HouseholdSchedule.label(days: chore.effectiveDays, dueTime: chore.effectiveDueTime)
                    if !schedule.isEmpty { Chip(text: schedule) }
                }
                if let impact = chore.impact, !impact.isEmpty {
                    Text(impact).font(.caption).foregroundStyle(Brand.muted)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .contextMenu {
            Button { onEdit() } label: { Label("Edit", systemImage: "pencil") }
            Button {
                Task { await toggleEnabled() }
            } label: {
                Label(chore.isEnabled ? "Pause reminders" : "Resume reminders",
                      systemImage: chore.isEnabled ? "pause.circle" : "play.circle")
            }
            Button(role: .destructive) {
                Task { await remove() }
            } label: { Label("Remove", systemImage: "trash") }
        }
    }

    private func toggleEnabled() async {
        do {
            try await withAPI { try await $0.setChoreEnabled(chore.id, enabled: !chore.isEnabled) }
            await reload()
        } catch {
            onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
    }

    private func remove() async {
        do {
            try await withAPI { try await $0.removeChore(chore.id) }
            await reload()
        } catch {
            onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
    }
}

// MARK: - Shopping

/// The shared shopping list — still-needed first, tap to check off, swipe to
/// remove, plus an inline add field and a "clear bought" sweep. Adds are capture
/// writes (queued off-tailnet).
struct ShoppingCard: View {
    let items: [ShoppingItem]
    let reload: () async -> Void
    let onError: (String) -> Void
    @State private var draft = ""

    private var hasGot: Bool { items.contains { $0.isGot } }

    var body: some View {
        Card {
            HStack {
                CardLabel(text: "Shopping list")
                Spacer()
                if hasGot {
                    AsyncButton {
                        try await withAPI { _ = try await $0.clearGotShopping() }
                        await reload()
                    } label: {
                        Text("Clear bought").font(.caption.weight(.medium))
                    } onError: { onError($0) }
                    .buttonStyle(.plain).tint(Brand.accent)
                }
            }
            HStack(spacing: 8) {
                TextField("Add something to buy", text: $draft)
                    .textFieldStyle(.plain)
                    .onSubmit { Task { await addDraft() } }
                AsyncButton { await addDraft() } label: {
                    Image(systemName: "plus.circle.fill").font(.title3).foregroundStyle(Brand.accent)
                } onError: { onError($0) }
                .buttonStyle(.plain)
                .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(10)
            .background(Brand.raise, in: RoundedRectangle(cornerRadius: 10))

            if items.isEmpty {
                Text("List's empty — add what you need above.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            } else {
                ForEach(items) { item in
                    SwipeToReveal(label: "Remove", systemImage: "trash", tint: Brand.danger,
                                  surface: Brand.card, cornerRadius: 8) {
                        // Surface a rejected removal instead of pretending it worked:
                        // only reload on success, so a failed delete leaves the row
                        // (SwipeToReveal settles it closed) and shows the error.
                        do {
                            try await withAPI { try await $0.removeShopping(item.id) }
                            await reload()
                        } catch {
                            onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
                        }
                    } content: {
                        ShoppingRow(item: item, reload: reload, onError: onError)
                    }
                }
            }
        }
    }

    private func addDraft() async {
        let text = draft.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        do {
            try await withAPI { try await $0.addShopping(item: text) }
            draft = ""
            await reload()
        } catch {
            onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
    }
}

struct ShoppingRow: View {
    let item: ShoppingItem
    let reload: () async -> Void
    let onError: (String) -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            AsyncButton {
                try await withAPI { try await $0.setShoppingGot(item.id, got: !item.isGot) }
                await reload()
            } label: {
                Image(systemName: item.isGot ? "checkmark.circle.fill" : "circle")
                    .font(.title3).foregroundStyle(item.isGot ? Brand.good : Brand.muted)
            } onError: { onError($0) }
            .buttonStyle(.plain)

            VStack(alignment: .leading, spacing: 2) {
                Text(item.item)
                    .font(.subheadline)
                    .foregroundStyle(item.isGot ? Brand.muted : Brand.nearWhite)
                    .strikethrough(item.isGot, color: Brand.muted)
                FlowRow(spacing: 6) {
                    if let spec = item.spec, !spec.isEmpty { Chip(text: spec) }
                    if let w = item.whereToBuy, !w.isEmpty { Chip(text: w, color: Brand.fyi) }
                    if let c = item.childName, !c.isEmpty { DomainPill(text: c) }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 6)
    }
}
