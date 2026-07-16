import SwiftUI

/// **Household settings** — the co-parent surfaces' opt-ins: the weekly
/// mental-load check-in schedule, the daily delta digest, and the load-balance
/// view. All three only matter once a second parent joins, so for a household of
/// one they're replaced by a gentle "applies once someone joins" note. Writes
/// `POST /household/{checkin,digest,balance}`; reached from the Household tab's
/// gear. Mirrors the Me ▸ Settings idiom (a Form pushed via NavigationLink).
struct HouseholdSettingsView: View {
    let checkin: Checkin?
    let digest: Digest?
    let balance: BalanceInfo?
    let shared: Bool
    let reload: () async -> Void

    @State private var digestOn: Bool
    @State private var balanceOn: Bool
    @State private var checkinOn: Bool
    @State private var day: Int
    @State private var time: Date
    @State private var error: String?
    @State private var savingCheckin = false
    @State private var checkinSaved = false
    /// Per-toggle write generation, so a stale failure can't revert a newer flip.
    @State private var writeGen: [String: Int] = [:]

    private static let weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    init(checkin: Checkin?, digest: Digest?, balance: BalanceInfo?,
         shared: Bool, reload: @escaping () async -> Void) {
        self.checkin = checkin
        self.digest = digest
        self.balance = balance
        self.shared = shared
        self.reload = reload
        _digestOn = State(initialValue: digest?.enabled ?? false)
        _balanceOn = State(initialValue: balance?.enabled ?? false)
        _checkinOn = State(initialValue: checkin?.enabled ?? false)
        _day = State(initialValue: checkin?.day ?? 6)   // default Sunday
        _time = State(initialValue: AvailableHours.date(from: checkin?.time ?? "19:00"))
    }

    var body: some View {
        Form {
            if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            if shared {
                checkinSection
                digestSection
                balanceSection
            } else {
                Section {
                    Text("These are co-parent settings — a weekly mental-load check-in, a daily \u{201C}what changed\u{201D} digest, and a shared load-balance view. They light up once someone joins your household.")
                        .font(.footnote).foregroundStyle(Brand.muted)
                }
            }
        }
        .brandScreen()
        .navigationTitle("Household settings")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: weekly check-in

    private var checkinSection: some View {
        Section {
            Toggle("Weekly check-in", isOn: $checkinOn.animation())
            if checkinOn {
                Picker("Day", selection: $day) {
                    ForEach(Array(Self.weekdays.enumerated()), id: \.offset) { idx, label in
                        Text(label).tag(idx)
                    }
                }
                DatePicker("Time", selection: $time, displayedComponents: .hourAndMinute)
            }
            AsyncButton {
                await saveCheckin()
            } label: {
                HStack {
                    Text("Save check-in")
                    if checkinSaved { Image(systemName: "checkmark").foregroundStyle(Brand.good) }
                }
            } onError: { error = $0 }
            .disabled(savingCheckin)
        } footer: {
            checkinFooter
        }
    }

    @ViewBuilder
    private var checkinFooter: some View {
        let responses = checkin?.responses.filter { ($0.response ?? "").isEmpty == false } ?? []
        if !responses.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text("This week").font(.caption2.weight(.semibold))
                ForEach(responses) { r in
                    Text("\(r.byName ?? "Someone"): \(label(for: r.response))")
                        .font(.caption2)
                }
            }
        } else {
            Text("A gentle once-a-week nudge to both parents — \u{201C}how has the invisible load felt for you?\u{201D} No scorekeeping.")
        }
    }

    private func label(for response: String?) -> String {
        switch response {
        case "light": return "felt light"
        case "ok", "balanced": return "felt balanced"
        case "heavy": return "felt heavy"
        default: return response ?? "—"
        }
    }

    private func saveCheckin() async {
        savingCheckin = true; defer { savingCheckin = false }
        do {
            let d: Int? = checkinOn ? day : nil
            let t: String? = checkinOn ? AvailableHours.hhmm(from: time) : nil
            try await withAPI { try await $0.setCheckin(enabled: checkinOn, day: d, time: t) }
            error = nil
            withAnimation { checkinSaved = true }
            await reload()
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                withAnimation { checkinSaved = false }
            }
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }

    // MARK: digest + balance (immediate toggles)

    private var digestSection: some View {
        Section {
            Toggle("Daily digest",
                   isOn: writeThrough("digest", $digestOn) { try await $0.setDigest(enabled: $1) })
        } footer: {
            Text("Once a day, catch up on what your co-parent changed on the sheet since you last looked. Silent when nothing's new.")
        }
    }

    private var balanceSection: some View {
        Section {
            Toggle("Load balance",
                   isOn: writeThrough("balance", $balanceOn) { try await $0.setBalance(enabled: $1) })
        } footer: {
            Text("A gentle, no-judgment view of who's been keeping the sheet up — sheet edits, stars, and chores done, plus the routines each parent carries.")
        }
    }

    /// Wrap a `Bool` state binding so flipping the toggle writes through `op`
    /// optimistically, reverting the local value (and surfacing the error) if the
    /// write fails — so the switch never lies about the server's state.
    ///
    /// Each flip bumps a per-toggle generation token; a failure only reverts when
    /// it's still the latest write for that toggle, so a slow earlier failure can't
    /// clobber a newer flip the user has since made (out-of-order results).
    private func writeThrough(_ key: String, _ source: Binding<Bool>,
                              _ op: @escaping (APIClient, Bool) async throws -> Void) -> Binding<Bool> {
        Binding(
            get: { source.wrappedValue },
            set: { newValue in
                source.wrappedValue = newValue
                let token = (writeGen[key] ?? 0) + 1
                writeGen[key] = token
                Task {
                    do {
                        try await withAPI { try await op($0, newValue) }
                        if writeGen[key] == token { error = nil }
                        await reload()
                    } catch {
                        // Ignore a stale failure the user has already superseded.
                        guard writeGen[key] == token else { return }
                        self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                        source.wrappedValue = !newValue
                    }
                }
            }
        )
    }
}
