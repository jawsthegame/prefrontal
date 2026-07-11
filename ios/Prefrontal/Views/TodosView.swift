import SwiftUI

struct TodosView: View {
    @State private var todos: [Todo] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var showAdd = false

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                if let error { ErrorBanner(message: error) }
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
            ToolbarItem(placement: .topBarTrailing) {
                Button { showAdd = true } label: { Image(systemName: "plus") }
            }
        }
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .sheet(isPresented: $showAdd) { AddTodoSheet { await load() } }
    }

    private func load() async {
        do {
            let items = try await withAPI { try await $0.todos() }
            todos = items.filter { $0.status == "open" }
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
                    HStack(spacing: 6) {
                        if let m = todo.estimateMinutes { Chip(text: "~\(Int(m))m") }
                        if let p = todo.priority { Chip(text: priorityLabel(p), color: priorityColor(p).opacity(0.2), fg: priorityColor(p)) }
                        if let d = todo.deadline, let short = deadlineShort(d) {
                            Chip(text: short, color: Brand.warn.opacity(0.18), fg: Brand.warn)
                        }
                        if todo.isStarted { Chip(text: "in progress", color: Brand.blue.opacity(0.2), fg: Brand.blue) }
                        if let c = todo.category { Chip(text: c) }
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
        HStack(spacing: 8) {
            if todo.isStarted {
                actionBtn("Pause", "pause.circle") { try await withAPI { try await $0.unstartTodo(todo.id) } }
            } else {
                actionBtn("Start", "play.circle") { try await withAPI { try await $0.startTodo(todo.id) } }
            }
            if todo.decomposition == nil {
                actionBtn("Break down", "list.bullet.indent") { try await withAPI { try await $0.decomposeTodo(todo.id) } }
            }
            actionBtn("Drop", "trash", role: .destructive) { try await withAPI { try await $0.closeTodo(todo.id, done: false) } }
        }
        .font(.caption)
    }

    private func actionBtn(_ title: String, _ icon: String, role: ButtonRole? = nil, _ action: @escaping () async throws -> Void) -> some View {
        AsyncButton(role: role) {
            try await action()
            await reload()
        } label: {
            Label(title, systemImage: icon).font(.caption)
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
