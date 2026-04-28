import SwiftUI

struct LibraryView: View {
    @EnvironmentObject var store: AppStore
    @State private var search = ""
    @State private var searchTask: Task<Void, Never>? = nil

    var body: some View {
        NavigationStack {
            List(store.tracks) { track in
                TrackRow(track: track)
            }
            .searchable(text: $search, prompt: "Title or artist")
            .onChange(of: search) { _, q in
                searchTask?.cancel()
                searchTask = Task {
                    try? await Task.sleep(for: .milliseconds(300))
                    guard !Task.isCancelled else { return }
                    await store.loadTracks(search: q.isEmpty ? nil : q)
                }
            }
            .refreshable { await store.loadTracks(search: search.isEmpty ? nil : search) }
            .navigationTitle("Library")
            .overlay {
                if store.isLoadingTracks { ProgressView() }
                else if store.tracks.isEmpty {
                    ContentUnavailableView.search(text: search)
                }
            }
            .task { await store.loadTracks(search: search.isEmpty ? nil : search) }
            .onDisappear {
                searchTask?.cancel()
                searchTask = nil
            }
        }
    }
}

struct TrackRow: View {
    let track: Track

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(track.displayTitle).font(.subheadline.weight(.medium)).lineLimit(1)
            HStack(spacing: 8) {
                Text(track.displayArtist).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if let bpm = track.bpm {
                    Text(String(format: "%.0f BPM", bpm)).font(.caption2).foregroundStyle(.secondary)
                }
                if let key = track.key {
                    Text(key).font(.caption2.weight(.semibold)).foregroundStyle(.accentColor)
                }
            }
        }
        .padding(.vertical, 2)
    }
}
