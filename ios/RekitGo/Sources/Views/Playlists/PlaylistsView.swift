import SwiftUI

struct PlaylistsView: View {
    @EnvironmentObject var store: AppStore
    @State private var showingCreate = false
    @State private var newName = ""

    var body: some View {
        NavigationStack {
            List {
                ForEach(store.playlists) { playlist in
                    NavigationLink(destination: PlaylistDetailView(playlist: playlist)) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(playlist.name).font(.subheadline.weight(.medium))
                                if let count = playlist.trackCount {
                                    Text("\(count) tracks").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
                .onDelete { idx in
                    let toDelete = idx.map { store.playlists[$0] }
                    Task { for p in toDelete { await store.deletePlaylist(p) } }
                }
            }
            .refreshable { await store.loadPlaylists() }
            .navigationTitle("Playlists")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showingCreate = true } label: { Image(systemName: "plus") }
                }
            }
            .alert("New Playlist", isPresented: $showingCreate) {
                TextField("Name", text: $newName)
                Button("Create") {
                    let name = newName.trimmingCharacters(in: .whitespaces)
                    if !name.isEmpty { Task { await store.createPlaylist(name: name) } }
                    newName = ""
                }
                Button("Cancel", role: .cancel) { newName = "" }
            }
            .task { await store.loadPlaylists() }
        }
    }
}

struct PlaylistDetailView: View {
    @EnvironmentObject var store: AppStore
    let playlist: Playlist
    @State private var detail: Playlist? = nil
    @State private var loading = true

    var tracks: [Track] { detail?.tracks ?? [] }

    var body: some View {
        List(tracks) { track in
            TrackRow(track: track)
                .swipeActions {
                    Button(role: .destructive) {
                        Task {
                            try? await store.api.removeTrackFromPlaylist(playlistId: playlist.id, trackId: track.id)
                            detail?.tracks?.removeAll { $0.id == track.id }
                        }
                    } label: { Label("Remove", systemImage: "trash") }
                }
        }
        .navigationTitle(playlist.name)
        .overlay { if loading { ProgressView() } }
        .task {
            if let p = try? await store.api.fetchPlaylist(playlist.id) { detail = p }
            loading = false
        }
    }
}
