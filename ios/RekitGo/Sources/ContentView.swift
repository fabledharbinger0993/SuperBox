import SwiftUI

struct ContentView: View {
    @EnvironmentObject var store: AppStore

    var body: some View {
        TabView {
            DownloadsView()
                .tabItem { Label("Downloads", systemImage: "arrow.down.circle") }

            LibraryView()
                .tabItem { Label("Library", systemImage: "music.note.list") }

            PlaylistsView()
                .tabItem { Label("Playlists", systemImage: "list.bullet") }

            ExportView()
                .tabItem { Label("Export", systemImage: "externaldrive") }

            SettingsView(isInitialSetup: false)
                .tabItem { Label("Settings", systemImage: "gear") }
        }
        .alert("Error", isPresented: .constant(store.error != nil), actions: {
            Button("OK") { store.error = nil }
        }, message: {
            Text(store.error ?? "")
        })
    }
}
