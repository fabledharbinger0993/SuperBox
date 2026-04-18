import SwiftUI

struct DownloadsView: View {
    @EnvironmentObject var store: AppStore
    @State private var showingNewDownload = false

    var body: some View {
        NavigationStack {
            Group {
                if store.jobs.isEmpty && !store.isLoadingJobs {
                    ContentUnavailableView("No Downloads",
                        systemImage: "arrow.down.circle",
                        description: Text("Paste a Bandcamp, Beatport, or Soundcloud link to download directly to your library."))
                } else {
                    List(store.jobs) { job in
                        DownloadJobRow(job: job)
                    }
                    .refreshable { await store.loadJobs() }
                }
            }
            .navigationTitle("Downloads")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showingNewDownload = true } label: {
                        Image(systemName: "plus")
                    }
                }
            }
            .sheet(isPresented: $showingNewDownload) {
                NewDownloadSheet()
                    .environmentObject(store)
            }
            .task { await store.loadJobs() }
        }
    }
}

struct DownloadJobRow: View {
    let job: DownloadJob

    var icon: String {
        switch job.status {
        case .done:        return "checkmark.circle.fill"
        case .failed:      return "xmark.circle.fill"
        case .downloading: return "arrow.down.circle"
        case .converting:  return "waveform"
        case .importing:   return "tray.and.arrow.down"
        case .queued:      return "clock"
        }
    }

    var iconColor: Color {
        switch job.status {
        case .done:   return .green
        case .failed: return .red
        default:      return .accentColor
        }
    }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).foregroundStyle(iconColor).font(.title3)
            VStack(alignment: .leading, spacing: 3) {
                Text(job.title ?? job.url)
                    .lineLimit(1)
                    .font(.subheadline.weight(.medium))
                if let artist = job.artist {
                    Text(artist).font(.caption).foregroundStyle(.secondary)
                }
                if job.status == .failed, let err = job.error {
                    Text(err).font(.caption).foregroundStyle(.red).lineLimit(2)
                } else if ![DownloadStatus.done, .failed, .queued].contains(job.status) {
                    ProgressView(value: Double(job.progress), total: 100)
                        .tint(.accentColor)
                }
            }
            Spacer()
            Text(job.format.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }
}

struct NewDownloadSheet: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) var dismiss

    @State private var url         = ""
    @State private var format      = "aiff"
    @State private var destination = ""
    @State private var folders:    [[String: Any]] = []

    let formats = ["aiff", "flac", "wav", "mp3"]

    var body: some View {
        NavigationStack {
            Form {
                Section("Source URL") {
                    TextField("Bandcamp / Beatport / Soundcloud link", text: $url)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                }
                Section("Format") {
                    Picker("Format", selection: $format) {
                        ForEach(formats, id: \.self) { Text($0.uppercased()).tag($0) }
                    }
                    .pickerStyle(.segmented)
                }
                Section("Destination") {
                    if folders.isEmpty {
                        TextField("Path on Mac (e.g. /Volumes/DJMT/New Drops)", text: $destination)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                    } else {
                        Picker("Folder", selection: $destination) {
                            ForEach(folders, id: \.description) { f in
                                if let path = f["path"] as? String {
                                    Text(path).tag(path)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("New Download")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Download") {
                        Task {
                            await store.enqueueDownload(url: url, destination: destination, format: format)
                            dismiss()
                        }
                    }
                    .disabled(url.isEmpty || destination.isEmpty)
                }
            }
            .task {
                if let f = try? await store.api.fetchFolders() { folders = f }
            }
        }
    }
}
