import SwiftUI

/// Which chart-editor sheet is up: a fresh star chart, or an edit of an existing
/// agreement (chart or a plain plan being given reward tiers).
enum ChartEditor: Identifiable {
    case create
    case edit(Agreement)
    var id: Int { if case let .edit(a) = self { return a.id } else { return 0 } }
}

/// Create a star chart, or edit an existing plan's reward tiers + award-prompt
/// schedule. Create posts `POST /household/agreements` (kind `reward`) then
/// `/tiers` (which is what makes it a chart) and, if enabled, `/prompt`. Edit
/// re-sends tiers/prompt for the existing agreement — the title + child are the
/// key, so they're fixed in edit mode. Advanced fields (custom prompt question)
/// stay on the web dashboard.
struct ChartEditorSheet: View {
    let mode: ChartEditor
    let children: [RosterMember]
    @Environment(\.dismiss) private var dismiss

    @State private var title: String
    @State private var childId: Int              // 0 = whole household
    @State private var tiers: String
    @State private var promptEnabled: Bool
    @State private var promptDays: Set<Int>
    @State private var promptTime: Date
    @State private var error: String?
    @State private var saving = false

    /// Set only in edit mode — the agreement id to re-target, and whether it
    /// already had an enabled prompt (so turning the toggle off writes a disable).
    private let editingId: Int?
    private let editChildName: String?
    private let wasPromptEnabled: Bool

    private static let weekdayLabels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    init(mode: ChartEditor, children: [RosterMember]) {
        self.mode = mode
        self.children = children
        if case let .edit(a) = mode {
            let p = a.structured?.prompt
            editingId = a.id
            editChildName = a.childName
            wasPromptEnabled = p?.enabled == true
            _title = State(initialValue: a.title)
            _childId = State(initialValue: 0)
            _tiers = State(initialValue: a.tiersSpec)
            _promptEnabled = State(initialValue: p?.enabled == true)
            _promptDays = State(initialValue: Set(p?.days ?? []))
            _promptTime = State(initialValue: AvailableHours.date(from: p?.time ?? "19:00"))
        } else {
            editingId = nil
            editChildName = nil
            wasPromptEnabled = false
            _title = State(initialValue: "")
            _childId = State(initialValue: 0)
            _tiers = State(initialValue: "")
            _promptEnabled = State(initialValue: false)
            _promptDays = State(initialValue: [])
            _promptTime = State(initialValue: AvailableHours.date(from: "19:00"))
        }
    }

    private var isEdit: Bool { editingId != nil }
    private var canSave: Bool {
        !title.trimmingCharacters(in: .whitespaces).isEmpty
            && !tiers.trimmingCharacters(in: .whitespaces).isEmpty
            && !(promptEnabled && promptDays.isEmpty)
            && !saving
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Chart") {
                    if isEdit {
                        LabeledContent("Name", value: title)
                        if let c = editChildName, !c.isEmpty { LabeledContent("For", value: c) }
                    } else {
                        TextField("Name (e.g. Bedtime routine)", text: $title)
                        Picker("For", selection: $childId) {
                            Text("Whole household").tag(0)
                            ForEach(children) { Text($0.name).tag($0.id) }
                        }
                    }
                }
                Section {
                    TextField("e.g. 7=movie night, 30=new LEGO", text: $tiers, axis: .vertical)
                        .lineLimit(1...3)
                } header: {
                    Text("Reward tiers")
                } footer: {
                    Text("One or more count=reward, comma-separated. Stars add up toward each reward.")
                }
                promptSection
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle(isEdit ? "Edit chart" : "New star chart")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await save() } }.disabled(!canSave)
                }
            }
        }
    }

    private var promptSection: some View {
        Section {
            Toggle("Daily reminder to award", isOn: $promptEnabled.animation())
            if promptEnabled {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Days").font(.caption).foregroundStyle(Brand.muted)
                    FlowRow(spacing: 6, lineSpacing: 6) {
                        ForEach(Array(Self.weekdayLabels.enumerated()), id: \.offset) { idx, label in
                            let on = promptDays.contains(idx)
                            Button {
                                if on { promptDays.remove(idx) } else { promptDays.insert(idx) }
                            } label: {
                                Text(label)
                                    .font(.caption2.weight(.semibold))
                                    .padding(.horizontal, 10).padding(.vertical, 5)
                                    .background(on ? Brand.accent : Brand.chip, in: Capsule())
                                    .foregroundStyle(on ? Brand.accentFg : Brand.muted)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                DatePicker("Time", selection: $promptTime, displayedComponents: .hourAndMinute)
            }
        } footer: {
            Text(promptEnabled
                 ? "Both parents get a one-tap \u{201C}did they earn a star?\u{201D} on the chosen days."
                 : "Optional — a nudge to remember to award stars.")
        }
    }

    private func save() async {
        saving = true; defer { saving = false }
        let tiersSpec = tiers.trimmingCharacters(in: .whitespaces)
        let days = promptDays.sorted()
        let time = AvailableHours.hhmm(from: promptTime)
        do {
            try await withAPI { client in
                let id: Int
                if let editingId {
                    id = editingId
                } else {
                    id = try await client.createAgreement(
                        title: title.trimmingCharacters(in: .whitespaces), kind: "reward", childId: childId
                    ).id
                }
                try await client.setStarTiers(id, tiers: tiersSpec)
                if promptEnabled {
                    try await client.setStarPrompt(id, enabled: true, days: days, time: time)
                } else if wasPromptEnabled {
                    // Was on, now off — persist the disable (server still needs a valid time).
                    try await client.setStarPrompt(id, enabled: false, days: days, time: time)
                }
            }
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
