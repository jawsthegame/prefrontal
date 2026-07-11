import SwiftUI

struct PanicView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var panic: Panic?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let error { ErrorBanner(message: error) }
                    if let p = panic {
                        Card {
                            CardLabel(text: "First step")
                            Text(p.firstStep ?? "Take a breath.")
                                .font(.title3.weight(.semibold)).foregroundStyle(Brand.nearWhite)
                            if let f = p.firstStepFor {
                                Text("for: \(f)").font(.footnote).foregroundStyle(Brand.teal)
                            }
                        }
                        bucket("Already behind", p.late, Brand.danger)
                        bucket("Bearing down soon", p.soon, Brand.warn)
                        bucket("Piling up", p.pilingUp, Brand.muted)
                    } else if error == nil {
                        ProgressView().tint(Brand.teal).frame(maxWidth: .infinity).padding(.top, 60)
                    }
                }
                .padding(16)
            }
            .brandScreen()
            .navigationTitle("Panic")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
            .task { await load() }
        }
    }

    @ViewBuilder private func bucket(_ title: String, _ items: [Panic.Item], _ color: Color) -> some View {
        if !items.isEmpty {
            Card {
                HStack { CardLabel(text: title); Spacer(); Chip(text: "\(items.count)", color: color.opacity(0.2), fg: color) }
                ForEach(items) { item in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: item.kind == "todo" ? "checklist" : "calendar")
                            .font(.caption).foregroundStyle(color).padding(.top, 2)
                        Text(item.title).font(.footnote).foregroundStyle(Brand.nearWhite)
                        Spacer()
                        if let w = item.when { Text(w).font(.caption2).foregroundStyle(Brand.muted) }
                    }
                }
            }
        }
    }

    private func load() async {
        do { panic = try await withAPI { try await $0.panic() }; error = nil }
        catch { self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription }
    }
}
