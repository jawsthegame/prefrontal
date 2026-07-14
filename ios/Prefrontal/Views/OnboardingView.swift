import SwiftUI
import UIKit

/// First-run walkthrough: **welcome → connect → notifications → done**.
/// Connect is QR-first (scan the code from your setup sheet, or tap the
/// `prefrontal://connect` link) with a manual URL+token fallback. See
/// `docs/design/ios-onboarding.md`.
struct OnboardingView: View {
    @EnvironmentObject var config: AppConfig
    @ObservedObject private var model = OnboardingModel.shared

    var body: some View {
        VStack(spacing: 0) {
            StepBar(current: model.step)
                .padding(.horizontal, 20)
                .padding(.top, 12)

            ScrollView {
                Group {
                    switch model.step {
                    case .welcome:       WelcomeStep()
                    case .connect:       ConnectStep()
                    case .notifications: NotificationsStep()
                    case .done:          DoneStep()
                    }
                }
                .padding(20)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
        }
        .background(Brand.bg.ignoresSafeArea())
        .animation(.easeInOut(duration: 0.25), value: model.step)
    }
}

// MARK: - Progress bar

private struct StepBar: View {
    let current: OnboardingModel.Step
    var body: some View {
        HStack(spacing: 6) {
            ForEach(OnboardingModel.Step.allCases, id: \.rawValue) { s in
                Capsule()
                    .fill(s.rawValue <= current.rawValue ? Brand.accent : Brand.line)
                    .frame(height: 4)
            }
        }
    }
}

// MARK: - Welcome

private struct WelcomeStep: View {
    @ObservedObject private var model = OnboardingModel.shared
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Spacer(minLength: 24)
            Image(systemName: "brain.head.profile")
                .font(.system(size: 44, weight: .semibold))
                .foregroundStyle(Brand.accentFg)
                .frame(width: 72, height: 72)
                .background(Brand.accent, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 10) {
                Text("Welcome to Prefrontal")
                    .font(.largeTitle.weight(.bold)).foregroundStyle(Brand.fg)
                Text("Your executive-function assistant. Let's connect this phone to your deployment — it takes about a minute.")
                    .font(.body).foregroundStyle(Brand.muted)
            }
            FeatureRow(icon: "qrcode.viewfinder", title: "Scan to connect",
                       detail: "Point your camera at the QR code on your setup sheet.")
            FeatureRow(icon: "bell.badge", title: "Get nudges",
                       detail: "Departure reminders, self-care checks, and panic mode on your phone.")
            FeatureRow(icon: "lock.shield", title: "Stays yours",
                       detail: "Talks only to your own server over your private network.")
            Spacer(minLength: 8)
            Button { model.advance() } label: { WideLabel("Get started") }
                .buttonStyle(PrimaryButtonStyle())
        }
    }
}

private struct FeatureRow: View {
    let icon: String, title: String, detail: String
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.title3).foregroundStyle(Brand.accent)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Brand.fg)
                Text(detail).font(.footnote).foregroundStyle(Brand.muted)
            }
        }
    }
}

// MARK: - Connect

private struct ConnectStep: View {
    @EnvironmentObject var config: AppConfig
    @ObservedObject private var model = OnboardingModel.shared

    @State private var url = ""
    @State private var token = ""
    @State private var showScanner = false
    @State private var showManual = false
    @State private var status: String?
    @State private var ok = false
    @State private var testing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Connect to your server")
                    .font(.title.weight(.bold)).foregroundStyle(Brand.fg)
                Text("Scan the QR code on your setup sheet, or tap its `prefrontal://` link. No sheet handy? Enter the details by hand.")
                    .font(.callout).foregroundStyle(Brand.muted)
            }

            Button { showScanner = true } label: {
                WideLabel("Scan QR code", systemImage: "qrcode.viewfinder")
            }
            .buttonStyle(PrimaryButtonStyle())

            DisclosureGroup(isExpanded: $showManual) {
                VStack(spacing: 12) {
                    LabeledField(label: "Server URL", placeholder: "https://…ts.net", text: $url, secure: false)
                        .keyboardType(.URL)
                    LabeledField(label: "Token", placeholder: "X-Prefrontal-Token", text: $token, secure: true)
                }
                .padding(.top, 8)
            } label: {
                Text("Enter details manually").font(.subheadline.weight(.medium)).foregroundStyle(Brand.fg)
            }
            .tint(Brand.accent)

            if let status {
                Text(status).font(.footnote).foregroundStyle(ok ? Brand.good : Brand.danger)
            }

            Button { Task { await testAndSave() } } label: {
                HStack(spacing: 8) {
                    if testing { ProgressView().controlSize(.small) }
                    Text("Connect")
                }.frame(maxWidth: .infinity)
            }
            .buttonStyle(PrimaryButtonStyle())
            .disabled(testing || url.isEmpty || token.isEmpty)
        }
        .onAppear(perform: seed)
        .onChange(of: model.incoming) { _, new in if let new { applyIncoming(new) } }
        .sheet(isPresented: $showScanner) {
            ScannerSheet { handleScan($0) }
        }
    }

    private func seed() {
        if url.isEmpty { url = config.baseURLString }
        if token.isEmpty { token = config.token }
        if let incoming = model.incoming { applyIncoming(incoming) }
    }

    private func applyIncoming(_ payload: ConnectPayload) {
        url = payload.baseURL
        if let t = payload.token { token = t }
        config.apply(payload)                       // keep ntfy hints for the next step
        model.incoming = nil
        showManual = true                           // reveal what was filled
        // A link with both URL and token can connect straight away.
        if !url.isEmpty && !token.isEmpty { Task { await testAndSave() } }
    }

    private func handleScan(_ raw: String) {
        showScanner = false
        guard let payload = ConnectPayload(string: raw) else {
            status = "That QR code isn't a Prefrontal connect code."
            ok = false
            return
        }
        applyIncoming(payload)
    }

    private func testAndSave() async {
        testing = true; status = nil; ok = false
        defer { testing = false }
        config.baseURLString = url.trimmingCharacters(in: .whitespaces)
        config.token = token.trimmingCharacters(in: .whitespaces)
        do {
            _ = try await withAPI { try await $0.selfCare() }
            ok = true; status = "Connected ✓"
            try? await Task.sleep(nanoseconds: 350_000_000)
            model.advance()
        } catch {
            ok = false
            status = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}

/// Camera sheet with a framing reticle and a manual-entry escape hatch.
private struct ScannerSheet: View {
    let onScan: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()
                if let error {
                    VStack(spacing: 12) {
                        Image(systemName: "camera.fill").font(.largeTitle).foregroundStyle(.white.opacity(0.7))
                        Text(error).font(.callout).foregroundStyle(.white).multilineTextAlignment(.center)
                            .padding(.horizontal, 32)
                    }
                } else {
                    QRScannerView(onScan: onScan) { error = $0 }
                        .ignoresSafeArea()
                    RoundedRectangle(cornerRadius: 20)
                        .stroke(.white.opacity(0.9), lineWidth: 3)
                        .frame(width: 220, height: 220)
                    VStack {
                        Spacer()
                        Text("Point at the QR code on your setup sheet")
                            .font(.footnote.weight(.medium)).foregroundStyle(.white)
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .background(.black.opacity(0.5), in: Capsule())
                            .padding(.bottom, 40)
                    }
                }
            }
            .navigationTitle("Scan code").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .toolbarColorScheme(.dark, for: .navigationBar)
        }
    }
}

// MARK: - Notifications

private struct NotificationsStep: View {
    @EnvironmentObject var config: AppConfig
    @ObservedObject private var model = OnboardingModel.shared
    @Environment(\.openURL) private var openURL

    @State private var granted: Bool?
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Turn on notifications")
                    .font(.title.weight(.bold)).foregroundStyle(Brand.fg)
                Text("Nudges arrive as native notifications. Allow Prefrontal to send them — that's the whole setup.")
                    .font(.callout).foregroundStyle(Brand.muted)
            }

            Card {
                CardLabel(text: "Allow Prefrontal alerts")
                Text("Lets Prefrontal push a nudge to your phone — one-tap actions right on the notification.")
                    .font(.footnote).foregroundStyle(Brand.muted)
                AsyncButton {
                    granted = await model.requestNotifications()
                } label: {
                    Label(granted == true ? "Notifications on" : "Enable notifications",
                          systemImage: granted == true ? "checkmark.circle.fill" : "bell.badge")
                }
                .buttonStyle(.bordered)
                .tint(granted == true ? Brand.good : Brand.accent)
                .disabled(granted == true)
                if granted == false {
                    Text("You said no for now — you can turn these on later in iOS Settings.")
                        .font(.caption).foregroundStyle(Brand.muted)
                }
            }

            // Free-signing dev builds (no APNs entitlement) can't receive native
            // push, so the server's dev-only ntfy shim delivers instead. This card
            // only appears when the connect payload carried an ntfy topic — which
            // it does only on such a dev box; a product build never shows it.
            if !config.ntfyTopic.isEmpty {
                Card {
                    CardLabel(text: "Dev build — subscribe ntfy")
                    LabeledValue(label: "Server", value: config.ntfyServer)
                    LabeledValue(label: "Topic", value: config.ntfyTopic)
                    Button {
                        UIPasteboard.general.string = config.ntfyTopic
                        copied = true
                    } label: { Label(copied ? "Copied" : "Copy topic", systemImage: copied ? "checkmark" : "doc.on.doc") }
                        .font(.subheadline).tint(Brand.accent)
                    HStack(spacing: 10) {
                        Button("Get ntfy") { openURL(URL(string: "https://apps.apple.com/app/ntfy/id1625396347")!) }
                            .buttonStyle(.bordered).tint(Brand.accent)
                        Button("Open in ntfy") {
                            let s = config.ntfyServer.replacingOccurrences(of: "https://", with: "")
                                .replacingOccurrences(of: "http://", with: "")
                            openURL(URL(string: "https://\(s)/\(config.ntfyTopic)")!)
                        }
                        .buttonStyle(.bordered).tint(Brand.accent)
                    }
                }
            }

            Button { model.advance() } label: { WideLabel("Continue") }
                .buttonStyle(PrimaryButtonStyle())
        }
    }
}

// MARK: - Done

private struct DoneStep: View {
    @EnvironmentObject var config: AppConfig
    @ObservedObject private var model = OnboardingModel.shared
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Spacer(minLength: 32)
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 64)).foregroundStyle(Brand.good)
            VStack(alignment: .leading, spacing: 10) {
                Text(config.displayName.isEmpty ? "You're all set" : "You're all set, \(config.displayName)")
                    .font(.largeTitle.weight(.bold)).foregroundStyle(Brand.fg)
                Text("This phone is connected. You'll see today's plan, todos, your calendar, and self-care under the tabs — and Panic is one tap away when you need it.")
                    .font(.body).foregroundStyle(Brand.muted)
            }
            FeatureRow(icon: "sun.max", title: "Today", detail: "What needs attention right now.")
            FeatureRow(icon: "widget.small", title: "Add the widget", detail: "Long-press your Home Screen to add the Prefrontal glance.")
            Spacer(minLength: 12)
            Button { model.finish() } label: { WideLabel("Start using Prefrontal") }
                .buttonStyle(PrimaryButtonStyle())
        }
    }
}

// MARK: - Small shared pieces

private struct WideLabel: View {
    let title: String
    var systemImage: String?
    init(_ title: String, systemImage: String? = nil) { self.title = title; self.systemImage = systemImage }
    var body: some View {
        Group {
            if let systemImage { Label(title, systemImage: systemImage) }
            else { Text(title) }
        }
        .frame(maxWidth: .infinity)
    }
}

private struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { StyledButton(configuration: configuration) }

    // `@Environment(\.isEnabled)` only resolves inside a View, not a ButtonStyle,
    // so the label lives in a nested view to pick up the disabled state. NOT
    // named `Body` — that collides with ButtonStyle's `Body` associated type.
    private struct StyledButton: View {
        let configuration: ButtonStyleConfiguration
        @Environment(\.isEnabled) private var enabled
        var body: some View {
            configuration.label
                .font(.headline)
                .padding(.vertical, 14)
                .frame(maxWidth: .infinity)
                .background(Brand.accent.opacity(enabled ? (configuration.isPressed ? 0.8 : 1) : 0.4),
                            in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .foregroundStyle(Brand.accentFg)
        }
    }
}

private struct LabeledField: View {
    let label: String, placeholder: String
    @Binding var text: String
    var secure: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            CardLabel(text: label)
            Group {
                if secure { SecureField(placeholder, text: $text) }
                else { TextField(placeholder, text: $text) }
            }
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .padding(10)
            .background(Brand.card, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Brand.line))
        }
    }
}

private struct LabeledValue: View {
    let label: String, value: String
    var body: some View {
        HStack {
            Text(label).font(.footnote).foregroundStyle(Brand.muted)
            Spacer()
            Text(value).font(.footnote.monospaced()).foregroundStyle(Brand.fg)
                .lineLimit(1).truncationMode(.middle)
        }
    }
}
