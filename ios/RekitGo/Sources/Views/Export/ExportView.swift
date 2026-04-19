import SwiftUI

struct ExportView: View {
    @EnvironmentObject var store: AppStore
    @State private var selectedPlaylists = Set<Int>()
    @State private var selectedDrive: Drive? = nil
    @State private var exportJobId: String? = nil
    @State private var exportJob: ExportJob? = nil
    @State private var exporting = false
    @State private var pollTask: Task<Void, Never>? = nil

    var canExport: Bool {
        !selectedPlaylists.isEmpty && selectedDrive != nil && !exporting
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Pioneer Drive") {
                    if store.drives.isEmpty {
                        Text("No Pioneer drives detected. Connect a USB drive and refresh.")
                            .foregroundStyle(.secondary).font(.caption)
                    } else {
                        ForEach(store.drives) { drive in
                            Button {
                                selectedDrive = drive
                            } label: {
                                HStack {
                                    VStack(alignment: .leading) {
                                        Text(drive.name).foregroundStyle(.primary)
                                        Text(drive.path).font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if selectedDrive?.id == drive.id {
                                        Image(systemName: "checkmark").foregroundStyle(.accentColor)
                                    }
                                }
                            }
                        }
                    }
                }

                Section("Playlists to Export") {
                    ForEach(store.playlists) { playlist in
                        Button {
                            if selectedPlaylists.contains(playlist.id) {
                                selectedPlaylists.remove(playlist.id)
                            } else {
                                selectedPlaylists.insert(playlist.id)
                            }
                        } label: {
                            HStack {
                                Text(playlist.name).foregroundStyle(.primary)
                                Spacer()
                                if selectedPlaylists.contains(playlist.id) {
                                    Image(systemName: "checkmark").foregroundStyle(.accentColor)
                                }
                            }
                        }
                    }
                }

                if let job = exportJob {
                    Section("Progress") {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(job.status.capitalized)
                                Spacer()
                                Text("\(job.progress)%").foregroundStyle(.secondary)
                            }
                            ProgressView(value: Double(job.progress), total: 100)
                            if let msg = job.message { Text(msg).font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                }

                Section {
                    Button(action: startExport) {
                        HStack {
                            if exporting { ProgressView().padding(.trailing, 4) }
                            Text("Export to Drive")
                        }
                    }
                    .disabled(!canExport)
                }
            }
            .navigationTitle("Export")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { Task { await store.loadDrives(); await store.loadPlaylists() } } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            .task { await store.loadDrives(); await store.loadPlaylists() }
            .onDisappear { cancelPolling() }
        }
    }

    private func startExport() {
        guard let drive = selectedDrive else { return }
        exporting = true
        Task {
            do {
                let jobId = try await store.api.startExport(
                    playlistIds: Array(selectedPlaylists),
                    drivePath: drive.path
                )
                exportJobId = jobId
                pollExport(jobId: jobId)
            } catch {
                store.error = error.localizedDescription
                exporting = false
            }
        }
    }

    private func pollExport(jobId: String) {
        cancelPolling()
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                if let job = try? await store.api.fetchExportJob(jobId) {
                    exportJob = job
                    if job.status == "done" || job.status == "failed" {
                        exporting = false
                        pollTask = nil
                        return
                    }
                }
            }
        }
    }

    private func cancelPolling() {
        pollTask?.cancel()
        pollTask = nil
    }
}
