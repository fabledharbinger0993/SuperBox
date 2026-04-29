import SwiftUI

@main
struct FableGoApp: App {
    @StateObject private var store = AppStore.shared

    var body: some Scene {
        WindowGroup {
            if store.api.config == nil {
                SettingsView(isInitialSetup: true)
                    .environmentObject(store)
            } else {
                ContentView()
                    .environmentObject(store)
            }
        }
    }
}
