import SwiftUI
import UIKit

/// The operator-only admin surface — the native counterpart to the web `/admin`
/// page. Provisions users (minting their token + a scannable connect QR),
/// manages tokens (rotate / disable / re-enable), sets Google sign-in emails,
/// and creates households + wires members in. Reached from the Me tab, which
/// only shows the entry when `/admin/whoami` reports the caller is an operator.
struct AdminView: View {
    @EnvironmentObject var config: AppConfig

    @State private var whoami: AdminWhoami?
    @State private var users: [AdminUser] = []
    @State private var households: [AdminHousehold] = []
    @State private var error: String?
    @State private var loaded = false
    // The one-time token reveal (from a create or a rotate), with its QR.
    @State private var reveal: Reveal?
    @State private var showDisabled = false

    /// A minted token to show once, tagged with why (new user / rotated) so the
    /// reveal card can label it.
    struct Reveal: Identifiable {
        let user: AdminUserCreated
        let note: String
        var id: String { user.handle + user.token }
    }

    /// handle → household name, for the per-user "🏠 …" badge.
    private var householdByHandle: [String: String] {
        var map: [String: String] = [:]
        for h in households { for m in h.members { map[m.handle] = h.name } }
        return map
    }

    private var disabledCount: Int { users.filter { !$0.isActive }.count }
    private var shownUsers: [AdminUser] { showDisabled ? users : users.filter(\.isActive) }

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let error { ErrorBanner(message: error) }
                if let whoami, !whoami.isOperator {
                    notOperatorCard
                } else {
                    AddUserCard(onCreated: { reveal = Reveal(user: $0, note: "new user"); await load() },
                                onError: { error = $0 })
                    if let reveal {
                        ConnectQRCard(reveal: reveal, baseURL: config.baseURLString) {
                            self.reveal = nil
                        }
                    }
                    usersCard
                    HouseholdsCard(households: households, onReload: { await load() },
                                   onError: { error = $0 })
                }
            }
            .padding(16)
        }
        .brandScreen()
        .navigationTitle("Admin")
        .refreshable { await load() }
        .task { if !loaded { await load() } }
    }

    // MARK: Users

    private var usersCard: some View {
        Card {
            CardLabel(text: "Users")
            Text("Rotate re-issues a token (old devices stop working); disable stops a token resolving.")
                .font(.caption).foregroundStyle(Brand.muted)
            if !loaded {
                ProgressView().controlSize(.small)
            } else if shownUsers.isEmpty {
                Text(users.isEmpty ? "No users yet." : "No active users.")
                    .font(.footnote).foregroundStyle(Brand.muted)
            } else {
                ForEach(shownUsers) { user in
                    Divider().overlay(Brand.line)
                    AdminUserRow(user: user,
                                 households: households,
                                 householdName: householdByHandle[user.handle],
                                 onReload: { await load() },
                                 onToken: { reveal = Reveal(user: $0, note: "rotated") },
                                 onError: { error = $0 })
                }
            }
            if disabledCount > 0 {
                Button(showDisabled ? "Hide \(disabledCount) disabled" : "Show \(disabledCount) disabled") {
                    showDisabled.toggle()
                }
                .font(.footnote).tint(Brand.muted).padding(.top, 4)
            }
        }
    }

    private var notOperatorCard: some View {
        Card {
            CardLabel(text: "Admin")
            Text("Your account isn't an operator, so user and household management isn't available. Ask an operator to grant it.")
                .font(.footnote).foregroundStyle(Brand.muted)
        }
    }

    private func load() async {
        do {
            let client = try await MainActor.run { try APIClient() }
            // whoami first: a non-operator (or a server without /admin) shouldn't
            // spew 403s from the list calls — gate on it.
            let who = try await client.adminWhoami()
            whoami = who
            if who.isOperator {
                async let u = client.adminUsers()
                async let h = client.adminHouseholds()
                users = try await u
                households = try await h
            }
            error = nil
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
        loaded = true
    }
}

// MARK: - Add a user

private struct AddUserCard: View {
    let onCreated: (AdminUserCreated) async -> Void
    let onError: (String) -> Void

    @State private var handle = ""
    @State private var name = ""
    @State private var email = ""
    @State private var isOperator = false

    var body: some View {
        Card {
            CardLabel(text: "Add a user")
            Text("Provisions a person and mints their access code (token), shown once. Add a Google email to let them Sign in with Google.")
                .font(.caption).foregroundStyle(Brand.muted)
            field("Handle (e.g. sam)", text: $handle, keyboard: .default)
            field("Display name (optional)", text: $name, keyboard: .default)
            field("Google email (optional)", text: $email, keyboard: .emailAddress)
            Toggle("Operator", isOn: $isOperator)
                .font(.subheadline).tint(Brand.accent)
            AsyncButton {
                let created = try await withAPI {
                    try await $0.adminCreateUser(
                        handle: handle.trimmingCharacters(in: .whitespaces),
                        displayName: name.trimmingCharacters(in: .whitespaces),
                        email: email.trimmingCharacters(in: .whitespaces),
                        isOperator: isOperator)
                }
                handle = ""; name = ""; email = ""; isOperator = false
                await onCreated(created)
            } label: {
                Label("Add user", systemImage: "person.badge.plus").frame(maxWidth: .infinity)
            } onError: { onError($0) }
            .buttonStyle(.borderedProminent).tint(Brand.accent)
            .disabled(handle.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private func field(_ prompt: String, text: Binding<String>, keyboard: UIKeyboardType) -> some View {
        TextField(prompt, text: text)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .keyboardType(keyboard)
            .padding(10)
            .background(Brand.bg, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.line))
    }
}

// MARK: - Token + connect-QR reveal

/// Shows a freshly-minted token once, alongside a scannable connect QR the new
/// phone's camera opens straight into the app. The QR encodes the deployment's
/// base URL + this token + the handle/name, so a scan lands fully connected.
private struct ConnectQRCard: View {
    let reveal: AdminView.Reveal
    let baseURL: String
    let onDismiss: () -> Void

    /// The `prefrontal://connect?…` link — the exact wire format the CLI's
    /// connect-link QR uses, so the app's scanner reads it identically.
    private var connectLink: String? {
        ConnectPayload(baseURL: baseURL,
                       token: reveal.user.token,
                       handle: reveal.user.handle,
                       displayName: reveal.user.displayName).url?.absoluteString
    }

    var body: some View {
        Card {
            HStack {
                CardLabel(text: "Save this now — shown once")
                Spacer()
                Button { onDismiss() } label: { Image(systemName: "xmark.circle.fill") }
                    .tint(Brand.muted)
            }
            Text("Access code for @\(reveal.user.handle) (\(reveal.note)):")
                .font(.footnote).foregroundStyle(Brand.muted)
            Text(reveal.user.token)
                .font(.system(.body, design: .monospaced).weight(.bold))
                .foregroundStyle(Brand.accent)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack {
                Button {
                    UIPasteboard.general.string = reveal.user.token
                } label: {
                    Label("Copy code", systemImage: "doc.on.doc")
                }
                .buttonStyle(.bordered).tint(Brand.accent)
                if let connectLink {
                    ShareLink(item: connectLink) {
                        Label("Share link", systemImage: "square.and.arrow.up")
                    }
                    .buttonStyle(.bordered).tint(Brand.accent)
                }
            }
            .font(.footnote)
            if let connectLink, let qr = QRCode.image(from: connectLink) {
                Divider().overlay(Brand.line)
                Text("Point the new phone's camera here to connect it.")
                    .font(.caption).foregroundStyle(Brand.muted)
                Image(uiImage: qr)
                    .interpolation(.none)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: 240)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(12)
                    .background(Color.white, in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityLabel("Connect QR code for \(reveal.user.handle)")
            }
        }
    }
}

// MARK: - One user row

private struct AdminUserRow: View {
    let user: AdminUser
    let households: [AdminHousehold]
    let householdName: String?
    let onReload: () async -> Void
    let onToken: (AdminUserCreated) -> Void
    let onError: (String) -> Void

    @State private var emailDraft = ""
    @State private var editingEmail = false
    @State private var confirm: Confirm?

    private enum Confirm: Identifiable, Equatable {
        case rotate, disable
        var id: Int { self == .rotate ? 0 : 1 }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(user.displayName?.isEmpty == false ? user.displayName! : user.handle)
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                if user.isOperator { Chip(text: "operator", color: Brand.accent) }
                if !user.isActive { Chip(text: user.status, color: Brand.danger) }
                if let householdName { Chip(text: "🏠 \(householdName)", color: Brand.warn) }
                Spacer(minLength: 0)
            }
            Text("@\(user.handle)")
                .font(.caption.monospaced()).foregroundStyle(Brand.muted)

            FlowRow(spacing: 8, lineSpacing: 8) {
                Button("Rotate token") { confirm = .rotate }
                    .buttonStyle(.bordered).tint(Brand.accent)
                if user.isActive {
                    Button("Disable", role: .destructive) { confirm = .disable }
                        .buttonStyle(.bordered).tint(Brand.danger)
                } else {
                    AsyncButton {
                        try await withAPI { try await $0.adminEnableUser(user.handle) }
                        await onReload()
                    } label: { Text("Re-enable") } onError: { onError($0) }
                    .buttonStyle(.bordered).tint(Brand.accent)
                }
                Button {
                    emailDraft = user.email ?? ""
                    editingEmail.toggle()
                } label: { Text(user.email?.isEmpty == false ? "Edit email" : "Add email") }
                    .buttonStyle(.bordered).tint(Brand.muted)
                householdMenu
            }
            .font(.footnote)

            if editingEmail { emailEditor }
        }
        .confirmationDialog("Rotate @\(user.handle)'s token? Their current code stops working.",
                            isPresented: rotateBinding, titleVisibility: .visible) {
            Button("Rotate token") {
                perform {
                    let rotated = try await withAPI { try await $0.adminRotateUser(user.handle) }
                    onToken(rotated)
                    await onReload()
                }
            }
        }
        .confirmationDialog("Disable @\(user.handle)? Their access code stops resolving.",
                            isPresented: disableBinding, titleVisibility: .visible) {
            Button("Disable", role: .destructive) {
                perform {
                    try await withAPI { try await $0.adminDisableUser(user.handle) }
                    await onReload()
                }
            }
        }
    }

    /// Run an async action, surfacing any error through the row's `onError`.
    private func perform(_ work: @escaping () async throws -> Void) {
        Task {
            do { try await work() }
            catch { onError((error as? LocalizedError)?.errorDescription ?? error.localizedDescription) }
        }
    }

    @ViewBuilder private var householdMenu: some View {
        if households.isEmpty {
            EmptyView()
        } else {
            Menu {
                ForEach(households) { h in
                    Button(h.name) {
                        perform {
                            try await withAPI {
                                try await $0.adminAddHouseholdMember(householdId: h.id, handle: user.handle)
                            }
                            await onReload()
                        }
                    }
                }
            } label: {
                Label(householdName == nil ? "Add to household" : "Move household", systemImage: "house")
            }
            .buttonStyle(.bordered).tint(Brand.muted)
        }
    }

    private var emailEditor: some View {
        HStack(spacing: 8) {
            TextField("email (blank to clear)", text: $emailDraft)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.emailAddress)
                .font(.footnote)
                .padding(8)
                .background(Brand.bg, in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Brand.line))
            AsyncButton {
                try await withAPI {
                    try await $0.adminSetUserEmail(user.handle, email: emailDraft.trimmingCharacters(in: .whitespaces))
                }
                editingEmail = false
                await onReload()
            } label: { Text("Save") } onError: { onError($0) }
            .buttonStyle(.bordered).tint(Brand.accent).font(.footnote)
        }
    }

    private var rotateBinding: Binding<Bool> {
        Binding(get: { confirm == .rotate }, set: { if !$0 { confirm = nil } })
    }
    private var disableBinding: Binding<Bool> {
        Binding(get: { confirm == .disable }, set: { if !$0 { confirm = nil } })
    }
}

// MARK: - Households

private struct HouseholdsCard: View {
    let households: [AdminHousehold]
    let onReload: () async -> Void
    let onError: (String) -> Void

    @State private var name = ""

    var body: some View {
        Card {
            CardLabel(text: "Households")
            Text("Two co-parents in one household share its sheet, chores, and star charts. Create one, then add each user to it above.")
                .font(.caption).foregroundStyle(Brand.muted)
            HStack(spacing: 8) {
                TextField("Household name (e.g. The Kims)", text: $name)
                    .autocorrectionDisabled()
                    .padding(10)
                    .background(Brand.bg, in: RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(Brand.line))
                AsyncButton {
                    try await withAPI { try await $0.adminCreateHousehold(name: name.trimmingCharacters(in: .whitespaces)) }
                    name = ""
                    await onReload()
                } label: { Text("Create") } onError: { onError($0) }
                .buttonStyle(.borderedProminent).tint(Brand.accent)
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            if households.isEmpty {
                Text("No households yet.").font(.footnote).foregroundStyle(Brand.muted)
            } else {
                ForEach(households) { h in
                    Divider().overlay(Brand.line)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(h.name).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                            Text("#\(h.id)").font(.caption.monospaced()).foregroundStyle(Brand.muted)
                        }
                        let members = h.members.map(\.label).joined(separator: ", ")
                        Text(members.isEmpty ? "No members yet." : "Members: \(members)")
                            .font(.caption).foregroundStyle(Brand.muted)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}
