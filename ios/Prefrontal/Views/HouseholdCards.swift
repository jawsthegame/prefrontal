import SwiftUI

// The read-leaning cards of the Household tab: star charts, appointments, the
// roster + reference facts, the load-balance view, the catch-up feed, and the
// create/join empty state. The interactive daily-driver cards (chores, shopping)
// live in HouseholdView.swift.

// MARK: - Today glance (embedded entry point)

/// A compact Household glance embedded on the **Today** tab — a tap-through into
/// the full `HouseholdView`. Self-loads only the *light*, side-effect-free reads
/// (`/household/shopping` + `/household/chores/done`) rather than the full sheet,
/// so it doesn't stamp `household_seen_at` (which would silently clear the delta
/// digest) or force a full server-side build just to render Today. It hides
/// itself entirely for a user in no household (those reads 404), so it's
/// noise-free discovery for co-parents only. Mirrors `BlockersCard`'s
/// self-loading, embedded pattern.
struct HouseholdTodayCard: View {
    @State private var choresRemaining = 0
    @State private var toBuy = 0
    @State private var hidden = false
    @State private var loaded = false

    var body: some View {
        Group {
            if !hidden, loaded {
                NavigationLink { HouseholdView() } label: { card }
                    .buttonStyle(.plain)
            }
        }
        .task { if !loaded { await load() } }
    }

    private var card: some View {
        Card {
            HStack(spacing: 8) {
                Image(systemName: "house.fill").foregroundStyle(Brand.accent)
                CardLabel(text: "Household")
                Spacer()
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(Brand.muted)
            }
            HStack(spacing: 8) {
                if choresRemaining > 0 {
                    Chip(text: "\(choresRemaining) chore\(choresRemaining == 1 ? "" : "s") today", color: Brand.warn)
                }
                if toBuy > 0 { Chip(text: "\(toBuy) to buy", color: Brand.fyi) }
                if choresRemaining == 0 && toBuy == 0 {
                    Text("All caught up. 🎉").font(.caption).foregroundStyle(Brand.muted)
                }
            }
        }
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let shop = client.shoppingList()
            async let chores = client.choresStatus()
            let items = try await shop
            let status = try await chores
            toBuy = items.filter { !$0.isGot }.count
            choresRemaining = status.remaining
            hidden = false
        } catch APIError.http(404, _) {
            hidden = true   // not in a household — hide the glance
        } catch {
            // A transient failure just leaves the glance absent; the full tab
            // surfaces real errors. Don't clutter Today with them.
            hidden = true
        }
        loaded = true
    }
}

// MARK: - Star charts / agreements

/// Standing behaviour plans. Charts (with a star ladder) show progress and a
/// one-tap "add a star"; plain agreements show their plain-language body.
struct ChartsCard: View {
    let agreements: [Agreement]
    let reload: () async -> Void
    let onAward: (Agreement) -> Void
    let onError: (String) -> Void

    var body: some View {
        if !agreements.isEmpty {
            Card {
                CardLabel(text: "Agreements & star charts")
                ForEach(agreements) { a in
                    if a.isChart {
                        ChartRow(agreement: a, reload: reload, onAward: onAward, onError: onError)
                    } else {
                        AgreementRow(agreement: a)
                    }
                    if a.id != agreements.last?.id { Divider().overlay(Brand.line) }
                }
            }
        }
    }
}

struct ChartRow: View {
    let agreement: Agreement
    let reload: () async -> Void
    let onAward: (Agreement) -> Void
    let onError: (String) -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(agreement.title).font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                HStack(spacing: 6) {
                    Text("⭐️ \(agreement.starTotal ?? 0)")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Brand.warn)
                    if let c = agreement.childName, !c.isEmpty { DomainPill(text: c) }
                }
                if let goal = agreement.nextGoal {
                    Text("\(goal.remaining) to go → \(goal.reward)")
                        .font(.caption).foregroundStyle(Brand.muted)
                } else if (agreement.starTotal ?? 0) > 0 {
                    Text("All rewards earned 🎉").font(.caption).foregroundStyle(Brand.good)
                }
            }
            Spacer(minLength: 0)
            // Quick +1; long-press-equivalent (a bigger grant / a note) via the sheet.
            AsyncButton {
                _ = try await withAPI { try await $0.awardStars(agreement.id) }
                await reload()
            } label: {
                Image(systemName: "star.circle.fill").font(.title2).foregroundStyle(Brand.warn)
            } onError: { onError($0) }
            .buttonStyle(.plain)
            .accessibilityLabel("Add a star")
            .contextMenu {
                Button { onAward(agreement) } label: { Label("Award stars…", systemImage: "star.leadinghalf.filled") }
            }
        }
        .padding(.vertical, 2)
    }
}

struct AgreementRow: View {
    let agreement: Agreement
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(agreement.title).font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                if let c = agreement.childName, !c.isEmpty { DomainPill(text: c) }
                if let k = agreement.kind, !k.isEmpty { Chip(text: k) }
            }
            if let body = agreement.body, !body.isEmpty {
                Text(body).font(.caption).foregroundStyle(Brand.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 2)
    }
}

// MARK: - Appointments

/// Upcoming kid appointments, soonest first.
struct AppointmentsCard: View {
    let appointments: [Appointment]
    let onAdd: () -> Void

    var body: some View {
        Card {
            HStack {
                CardLabel(text: "Upcoming appointments")
                Spacer()
                Button { onAdd() } label: { Image(systemName: "plus").font(.footnote.weight(.semibold)) }
                    .tint(Brand.accent)
                    .accessibilityLabel("Add appointment")
            }
            if appointments.isEmpty {
                Text("Nothing in the next couple of weeks.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            } else {
                ForEach(appointments) { appt in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "calendar").foregroundStyle(Brand.accent).padding(.top, 2)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(appt.title).font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                            HStack(spacing: 6) {
                                Text(appt.when).font(.caption).foregroundStyle(Brand.muted)
                                if let loc = appt.location, !loc.isEmpty {
                                    Text("· \(loc)").font(.caption).foregroundStyle(Brand.muted)
                                }
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 2)
                }
            }
        }
    }
}

// MARK: - Roster + facts

/// The kids and pets roster with each member's reference facts — editable. The
/// household-wide ("Everyone") block leads, then each kid, then each pet. A ＋ on
/// a member adds a fact; a long-press on a fact edits or removes it. Roster adds
/// (child/pet) route up through the toolbar menu.
struct RosterCard: View {
    let sheet: HouseholdSheet
    let vocab: Vocab?
    let reload: () async -> Void
    let onAddChild: () -> Void
    let onAddPet: () -> Void
    @State private var factEditor: FactEditor?

    private func block(_ blocks: [FactBlock], childId: Int) -> FactBlock? {
        blocks.first { $0.childId == childId }
    }

    /// The category keys the add-fact picker offers — server vocab, or the known
    /// set as a fallback if the payload didn't carry it (nil *or* empty, so the
    /// picker always has options and the seeded selection matches a tag).
    private var categoryKeys: [String] {
        let keys = vocab?.factCategories ?? []
        return keys.isEmpty
            ? ["sizes", "routine", "food", "health", "school", "contact", "location", "services"]
            : keys
    }

    var body: some View {
        Card {
            HStack {
                CardLabel(text: "Kids & pets")
                Spacer()
                Menu {
                    Button { onAddChild() } label: { Label("Add child", systemImage: "figure.child") }
                    Button { onAddPet() } label: { Label("Add pet", systemImage: "pawprint") }
                } label: {
                    Image(systemName: "plus").font(.footnote.weight(.semibold))
                }
                .tint(Brand.accent)
            }

            // Household-wide facts (child_id 0) — always shown so there's a home
            // for things like trash day, the home address, or the Wi-Fi.
            member(childId: 0, name: "Everyone", tag: nil,
                   categories: block(sheet.perChild, childId: 0)?.categories ?? [])
            if !sheet.children.isEmpty || !sheet.pets.isEmpty { Divider().overlay(Brand.line) }

            ForEach(sheet.children) { child in
                member(childId: child.id, name: child.name, tag: birthdayTag(child.birthday),
                       categories: block(sheet.perChild, childId: child.id)?.categories ?? [])
                if child.id != sheet.children.last?.id || !sheet.pets.isEmpty { Divider().overlay(Brand.line) }
            }
            ForEach(sheet.pets) { pet in
                member(childId: pet.id, name: pet.name, tag: pet.species ?? birthdayTag(pet.birthday),
                       categories: block(sheet.perPet, childId: pet.id)?.categories ?? [])
                if pet.id != sheet.pets.last?.id { Divider().overlay(Brand.line) }
            }
        }
        .sheet(item: $factEditor, onDismiss: { Task { await reload() } }) { ed in
            FactEditorSheet(target: ed, categories: categoryKeys)
        }
    }

    private func member(childId: Int, name: String, tag: String?, categories: [FactCategory]) -> some View {
        MemberFacts(
            name: name, tag: tag, categories: categories,
            onAdd: { factEditor = FactEditor(childId: childId, memberName: name,
                                             category: nil, item: nil, value: nil) },
            onEdit: { cat, item in
                factEditor = FactEditor(childId: childId, memberName: name,
                                        category: cat, item: item.item, value: item.value)
            }
        )
    }

    private func birthdayTag(_ birthday: String?) -> String? {
        guard let b = birthday, !b.isEmpty else { return nil }
        return "🎂 \(b)"
    }
}

/// One roster member's name + reference facts, grouped by category. The ＋ adds a
/// fact for this member; a long-press on a fact edits or removes it.
struct MemberFacts: View {
    let name: String
    let tag: String?
    let categories: [FactCategory]
    let onAdd: () -> Void
    let onEdit: (String, FactItem) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(name).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                if let tag { Chip(text: tag) }
                Spacer(minLength: 4)
                Button { onAdd() } label: { Image(systemName: "plus.circle").font(.subheadline) }
                    .buttonStyle(.plain).tint(Brand.accent)
                    .accessibilityLabel("Add a fact for \(name)")
            }
            if categories.isEmpty {
                Text("No facts saved yet — add one with ＋.").font(.caption).foregroundStyle(Brand.muted)
            } else {
                ForEach(categories) { cat in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(cat.label.uppercased())
                            .font(.caption2.weight(.bold)).tracking(0.5).foregroundStyle(Brand.muted)
                        ForEach(cat.items) { item in
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(item.item).font(.caption).foregroundStyle(Brand.muted)
                                Text(item.value ?? "—").font(.caption).foregroundStyle(Brand.fg)
                                Spacer(minLength: 0)
                            }
                            .contentShape(Rectangle())
                            .contextMenu {
                                Button { onEdit(cat.category, item) } label: {
                                    Label("Edit", systemImage: "pencil")
                                }
                            }
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 2)
    }
}

// MARK: - Load balance

/// The opt-in "who's keeping the sheet up" view: the doing tally (edits, stars,
/// chores done this window) and, when present, the carrying tally (routines each
/// parent is accountable for). Framed as shares with no judgment.
struct BalanceCard: View {
    let balance: BalanceInfo
    let view: BalanceView

    var body: some View {
        Card {
            HStack {
                CardLabel(text: "Load balance")
                Spacer()
                if let d = balance.windowDays { Text("last \(d)d").font(.caption2).foregroundStyle(Brand.muted) }
            }
            shareBars(members: view.members, caption: view.caption)
            if let carrying = view.carrying, carrying.total > 0 {
                Divider().overlay(Brand.line)
                Text("Carrying (routines)").font(.caption).foregroundStyle(Brand.muted)
                shareBars(members: carrying.members, caption: carrying.caption)
            }
        }
    }

    @ViewBuilder
    private func shareBars(members: [ShareMember], caption: String?) -> some View {
        ForEach(members) { m in
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(m.name).font(.subheadline).foregroundStyle(Brand.nearWhite)
                    Spacer()
                    Text("\(m.share)%").font(.caption.weight(.semibold)).monospacedDigit()
                        .foregroundStyle(Brand.muted)
                }
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Brand.chip)
                        Capsule().fill(Brand.accent.opacity(0.5))
                            .frame(width: geo.size.width * CGFloat(m.share) / 100)
                    }
                }
                .frame(height: 8)
            }
        }
        if let caption, !caption.isEmpty {
            Text(caption).font(.caption).foregroundStyle(Brand.muted)
        }
    }
}

// MARK: - Recently changed (catch-up feed)

/// The load surface: what changed on the sheet lately, newest first. A small
/// note flags the other parent's still-unseen changes when the digest is on.
struct RecentlyChangedCard: View {
    let changes: [HouseholdChange]
    let digest: Digest?

    var body: some View {
        if !changes.isEmpty {
            Card {
                CardLabel(text: "Recently changed")
                if let d = digest, d.enabled, d.unseen > 0 {
                    Text("\(d.unseen) change\(d.unseen == 1 ? "" : "s") you haven't seen")
                        .font(.caption).foregroundStyle(Brand.accent)
                }
                ForEach(changes) { change in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(Brand.line).frame(width: 6, height: 6).padding(.top, 6)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(change.what).font(.caption).foregroundStyle(Brand.fg)
                            Text([change.who, change.when].compactMap { $0 }
                                .filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.caption2).foregroundStyle(Brand.muted)
                        }
                        Spacer(minLength: 0)
                    }
                }
            }
        }
    }
}

// MARK: - Empty state (no household yet)

/// Shown when the caller belongs to no household (the sheet 404s): create one, or
/// join an existing one with a co-parent's invite code.
struct HouseholdEmptyState: View {
    let reload: () async -> Void
    @State private var name = ""
    @State private var code = ""
    @State private var error: String?
    @State private var joined: String?

    var body: some View {
        VStack(spacing: 14) {
            Card {
                HStack {
                    Image(systemName: "house").foregroundStyle(Brand.accent).font(.title2)
                    Text("Set up a household").font(.title3.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                }
                Text("A shared space for co-parenting — chores, a shopping list, kids' details, and star charts, in sync with your partner.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            }

            if let error { ErrorBanner(message: error) }
            if let joined {
                Card {
                    Label("Joined \(joined)", systemImage: "checkmark.seal.fill").foregroundStyle(Brand.good)
                }
            }

            Card {
                CardLabel(text: "Start a household")
                TextField("Household name (e.g. The Rivera family)", text: $name)
                    .padding(10).background(Brand.raise, in: RoundedRectangle(cornerRadius: 10))
                AsyncButton {
                    let n = name.trimmingCharacters(in: .whitespaces)
                    guard !n.isEmpty else { return }
                    try await withAPI { try await $0.createHousehold(name: n) }
                    await reload()
                } label: {
                    Text("Create").frame(maxWidth: .infinity)
                } onError: { error = $0 }
                .buttonStyle(.borderedProminent).tint(Brand.accent)
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
            }

            Card {
                CardLabel(text: "Join with a code")
                TextField("Invite code (e.g. PLUM-7F2Q)", text: $code)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .padding(10).background(Brand.raise, in: RoundedRectangle(cornerRadius: 10))
                AsyncButton {
                    let c = code.trimmingCharacters(in: .whitespaces)
                    guard !c.isEmpty else { return }
                    let result = try await withAPI { try await $0.redeemInvite(code: c) }
                    joined = result.householdName ?? "your household"
                    await reload()
                } label: {
                    Text("Join").frame(maxWidth: .infinity)
                } onError: { error = $0 }
                .buttonStyle(.bordered).tint(Brand.accent)
                .disabled(code.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
    }
}
