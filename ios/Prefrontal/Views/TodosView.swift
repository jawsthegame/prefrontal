import SwiftUI

struct TodosView: View {
    @State private var todos: [Todo] = []
    @State private var clarifyCount = 0
    @State private var error: String?
    @State private var loaded = false
    @State private var showAdd = false

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                if let error { ErrorBanner(message: error) }
                if clarifyCount > 0 { clarifyBanner }
                if todos.isEmpty && loaded && error == nil {
                    Text("No open todos. Nice.").foregroundStyle(Brand.muted).padding(.top, 40)
                }
                ForEach(todos) { todo in
                    TodoRow(todo: todo, reload: load, onError: { error = $0 })
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Todos")
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                NavigationLink { ClarifyView() } label: { Image(systemName: "questionmark.bubble") }
                    .accessibilityLabel("Clarify")
                Button { showAdd = true } label: { Image(systemName: "plus") }
            }
        }
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(isPresented: $showAdd) { AddTodoSheet { await load() } }
    }

    /// Appears when the ambiguity sweep has queued questions — a nudge into the
    /// Clarify screen to hone the vague items into startable steps.
    private var clarifyBanner: some View {
        NavigationLink { ClarifyView() } label: {
            HStack(spacing: 10) {
                Image(systemName: "questionmark.bubble").foregroundStyle(Brand.accent)
                Text("\(clarifyCount) to clarify — hone vague items into a first step")
                    .font(.footnote).foregroundStyle(Brand.fg)
                Spacer(minLength: 4)
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(Brand.muted)
            }
            .padding(10)
            .background(Brand.accent.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.accent.opacity(0.35)))
        }
        .buttonStyle(.plain)
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            async let items = client.todos()
            async let clar = client.clarifications()
            todos = (try await items).filter { $0.status == "open" }
            // Best-effort: the clarify banner is a nicety, not worth failing the list.
            clarifyCount = (try? await clar)?.clarifications.count ?? 0
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

struct TodoRow: View {
    let todo: Todo
    let reload: () async -> Void
    let onError: (String) -> Void
    @State private var expanded = false
    @State private var showDelegate = false

    var body: some View {
        Card {
            HStack(alignment: .top, spacing: 10) {
                AsyncButton {
                    try await withAPI { try await $0.closeTodo(todo.id, done: true) }
                    await reload()
                } label: {
                    Image(systemName: "circle").font(.title3).foregroundStyle(Brand.teal)
                } onError: { onError($0) }

                VStack(alignment: .leading, spacing: 6) {
                    Text(todo.title).font(.subheadline.weight(.medium)).foregroundStyle(Brand.nearWhite)
                    FlowRow(spacing: 6) {
                        if todo.isStarted { Chip(text: "in progress", color: Brand.good) }
                        if let p = todo.priority, p >= 2 { Chip(text: priorityLabel(p), color: priorityColor(p)) }
                        if let d = todo.deadline, let short = deadlineShort(d) { Chip(text: short, color: Brand.warn) }
                        if let m = todo.estimateMinutes { Chip(text: "~\(Int(m))m") }
                        if let dom = todo.domain, !dom.isEmpty { DomainPill(text: dom) }
                        if let c = todo.category, c != todo.domain { Chip(text: c) }
                        if let g = todo.delegation { Chip(text: g.label, color: delegColor(g.status)) }
                    }
                    if expanded { detail }
                }
                Spacer(minLength: 0)
                Button { withAnimation { expanded.toggle() } } label: {
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.caption).foregroundStyle(Brand.muted)
                }
            }
        }
        .sheet(isPresented: $showDelegate) {
            DelegateSheet(todoId: todo.id, reload: reload)
        }
    }

    @ViewBuilder private var detail: some View {
        Divider().overlay(Brand.line)
        if let dec = todo.decomposition, !dec.allSteps.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(dec.allSteps, id: \.index) { step in
                    AsyncButton {
                        try await withAPI { try await $0.markStepDone(todo.id, step: step.index) }
                        await reload()
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: step.done ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(step.done ? Brand.ok : Brand.muted)
                            Text(step.text)
                                .font(.footnote)
                                .strikethrough(step.done)
                                .foregroundStyle(step.done ? Brand.muted : Brand.nearWhite)
                            Spacer()
                        }
                    } onError: { onError($0) }
                }
            }
        }
        // A wrapping row: four bordered buttons don't fit one line on a narrow
        // phone, so let them flow onto a second line at natural width rather than
        // getting crushed into blobs with letter-wrapped labels.
        FlowRow(spacing: 8, lineSpacing: 8) {
            if todo.isStarted {
                actionBtn("Pause", "pause.circle") { try await withAPI { try await $0.unstartTodo(todo.id) } }
            } else {
                actionBtn("Start", "play.circle") { try await withAPI { try await $0.startTodo(todo.id) } }
            }
            if todo.decomposition == nil {
                actionBtn("Break down", "list.bullet.indent") { try await withAPI { try await $0.decomposeTodo(todo.id) } }
            }
            Button { showDelegate = true } label: {
                Label(todo.delegation == nil ? "Delegate" : "Re-delegate", systemImage: "person.wave.2")
                    .font(.caption).lineLimit(1)
            }
            .buttonStyle(.bordered).tint(Brand.blue)
            actionBtn("Drop", "trash", role: .destructive) { try await withAPI { try await $0.closeTodo(todo.id, done: false) } }
        }
        .font(.caption)
        delegationPanel
    }

    @ViewBuilder private var delegationPanel: some View {
        if let g = todo.delegation {
            Divider().overlay(Brand.line)
            VStack(alignment: .leading, spacing: 8) {
                if g.isWorking {
                    Label("Reading your context and prepping — you'll get a heads-up when it's ready.",
                          systemImage: "brain.head.profile")
                        .font(.caption).foregroundStyle(Brand.muted)
                } else {
                    if let brief = g.brief, !brief.isEmpty {
                        Text(brief).font(.footnote).foregroundStyle(Brand.nearWhite)
                    }
                    if let actions = g.actions?.filter({ !($0.text ?? "").isEmpty }), !actions.isEmpty {
                        ForEach(Array(actions.enumerated()), id: \.offset) { _, a in
                            let text = a.text ?? ""
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: a.mine == true ? "person.fill" : "person")
                                    .font(.caption2).foregroundStyle(a.mine == true ? Brand.teal : Brand.muted)
                                Text(text).font(.caption).foregroundStyle(Brand.nearWhite)
                                Spacer(minLength: 0)
                                if a.mine == true {
                                    AsyncButton {
                                        try await withAPI { try await $0.addTodo(title: text) }
                                        await reload()
                                    } label: { Text("＋ Todo").font(.caption2) } onError: { onError($0) }
                                    .buttonStyle(.borderless).tint(Brand.teal)
                                }
                            }
                        }
                    }
                    if let drafts = g.drafts, !drafts.isEmpty {
                        ForEach(Array(drafts.enumerated()), id: \.offset) { _, dr in
                            VStack(alignment: .leading, spacing: 2) {
                                let head = [dr.channel, dr.to, dr.subject].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
                                if !head.isEmpty { Text(head).font(.caption2.weight(.semibold)).foregroundStyle(Brand.muted) }
                                if let b = dr.body, !b.isEmpty {
                                    Text(b).font(.caption2).foregroundStyle(Brand.nearWhite).lineLimit(8)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                            .background(Brand.raise, in: RoundedRectangle(cornerRadius: 8))
                        }
                    }
                    if g.canReturn {
                        actionBtn("Mark returned", "arrow.uturn.left") {
                            try await withAPI { try await $0.returnDelegation(todo.id) }
                        }
                    }
                }
                if g.status == "failed", let d = g.detail, !d.isEmpty {
                    Text(d).font(.caption2).foregroundStyle(Brand.warn)
                }
            }
        }
    }

    private func delegColor(_ status: String) -> Color {
        switch status {
        case "prepped", "forwarded": return Brand.good
        case "failed": return Brand.warn
        default: return Brand.muted
        }
    }

    private func actionBtn(_ title: String, _ icon: String, role: ButtonRole? = nil, _ action: @escaping () async throws -> Void) -> some View {
        AsyncButton(role: role) {
            try await action()
            await reload()
        } label: {
            Label(title, systemImage: icon).font(.caption).lineLimit(1)
        } onError: { onError($0) }
        .buttonStyle(.bordered)
        .tint(role == .destructive ? Brand.danger : Brand.teal)
    }

    private func priorityLabel(_ p: Int) -> String { ["someday", "low", "med", "high"][max(0, min(3, p))] }
    private func priorityColor(_ p: Int) -> Color { p >= 3 ? Brand.danger : (p == 2 ? Brand.warn : Brand.muted) }

    private func deadlineShort(_ s: String) -> String? {
        guard let d = PFDate.parse(s) else { return nil }
        let f = DateFormatter(); f.setLocalizedDateFormatFromTemplate("MMM d")
        return "due " + f.string(from: d)
    }
}

struct AddTodoSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("What needs doing?", text: $title, axis: .vertical)
                        .lineLimit(1...4)
                } footer: {
                    Text("Prefrontal infers estimate, priority, and deadline from the wording — e.g. \"email Sam by Friday, ~10 min\".")
                }
                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle("Add todo")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") { Task { await save() } }
                        .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func save() async {
        saving = true; defer { saving = false }
        do {
            try await withAPI { try await $0.addTodo(title: title.trimmingCharacters(in: .whitespaces)) }
            await onSaved()
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}

/// Hand a todo to an assistant: the in-app **AI agent** (writes a brief + drafts +
/// action items back onto the todo) or **email a human VA**. Mirrors the web
/// dashboard's delegate popover.
struct DelegateSheet: View {
    let todoId: Int
    let reload: () async -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var handler = "agent"
    @State private var destination = ""
    @State private var note = ""
    @State private var context = ""
    @State private var recipients: [String] = []
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Who does the prep?") {
                    Picker("Handler", selection: $handler) {
                        Text("AI agent").tag("agent")
                        Text("Email a VA").tag("email")
                    }
                    .pickerStyle(.segmented)
                    Text(handler == "agent"
                         ? "The in-app AI writes a research brief, any draft messages, and action items back onto this todo."
                         : "Emails that same brief to a human assistant over your mail account.")
                        .font(.caption).foregroundStyle(Brand.muted)
                }

                if handler == "email" {
                    Section("Assistant's email") {
                        TextField("va@example.com", text: $destination)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                            .keyboardType(.emailAddress)
                        if !recipients.isEmpty {
                            Menu("Recent recipients") {
                                ForEach(recipients, id: \.self) { r in
                                    Button(r) { destination = r }
                                }
                            }
                            .font(.footnote)
                        }
                        TextField("Optional cover note", text: $note, axis: .vertical).lineLimit(1...3)
                    }
                }

                Section("Context (optional)") {
                    TextField("Paste anything that helps — a transcript, notes, output from another tool.",
                              text: $context, axis: .vertical)
                        .lineLimit(2...8)
                }

                if let error { Section { Text(error).foregroundStyle(Brand.danger).font(.footnote) } }
            }
            .brandScreen()
            .navigationTitle("Delegate")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Hand off") { Task { await submit() } }
                        .disabled(saving || (handler == "email" && destination.trimmingCharacters(in: .whitespaces).isEmpty))
                }
            }
            .task { recipients = (try? await withAPI { try await $0.delegateRecipients() }) ?? [] }
        }
        .presentationDetents([.medium, .large])
    }

    private func submit() async {
        saving = true; defer { saving = false }
        do {
            try await withAPI {
                try await $0.delegateTodo(todoId, handler: handler,
                                          destination: destination, context: context, note: note)
            }
            await reload()
            dismiss()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
