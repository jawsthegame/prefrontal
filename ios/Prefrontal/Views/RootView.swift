import SwiftUI

struct RootView: View {
    @EnvironmentObject var config: AppConfig

    var body: some View {
        if config.isConfigured {
            MainTabs()
        } else {
            NavigationStack { SettingsView(isOnboarding: true) }
        }
    }
}

struct MainTabs: View {
    @State private var showPanic = false

    var body: some View {
        TabView {
            NavigationStack { TodayView(showPanic: $showPanic) }
                .tabItem { Label("Today", systemImage: "sun.max") }
            NavigationStack { TodosView() }
                .tabItem { Label("Todos", systemImage: "checklist") }
            NavigationStack { CalendarView() }
                .tabItem { Label("Calendar", systemImage: "calendar") }
            NavigationStack { MeView() }
                .tabItem { Label("Me", systemImage: "person.crop.circle") }
        }
        .sheet(isPresented: $showPanic) { PanicView() }
    }
}
