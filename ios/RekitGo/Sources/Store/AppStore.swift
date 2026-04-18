import Foundation
import Combine

@MainActor
final class AppStore: ObservableObject {

    static let shared = AppStore()

    let api = APIClient.shared

    // MARK: State

    @Published var tracks:    [Track]       = []
    @Published var playlists: [Playlist]    = []
    @Published var jobs:      [DownloadJob] = []
    @Published var drives:    [Drive]       = []

    @Published var isLoadingTracks:    Bool = false
    @Published var isLoadingPlaylists: Bool = false
    @Published var isLoadingJobs:      Bool = false

    @Published var error: String? = nil

    // MARK: Init

    init() {
        api.onEvent = { [weak self] json in
            self?.handleWSEvent(json)
        }
        if api.config != nil {
            api.connectWebSocket()
        }
    }

    // MARK: Downloads

    func loadJobs() async {
        isLoadingJobs = true
        defer { isLoadingJobs = false }
        do { jobs = try await api.fetchJobs() }
        catch { self.error = error.localizedDescription }
    }

    func enqueueDownload(url: String, destination: String, format: String) async {
        do {
            let jobId = try await api.enqueueDownload(url: url, destination: destination, format: format)
            // Optimistically add a placeholder job
            let placeholder = DownloadJob(
                jobId: jobId, url: url, destination: destination, format: format,
                status: .queued, progress: 0, title: nil, artist: nil, filePath: nil, error: nil
            )
            jobs.insert(placeholder, at: 0)
        } catch {
            self.error = error.localizedDescription
        }
    }

    // MARK: Library

    func loadTracks(search: String? = nil) async {
        isLoadingTracks = true
        defer { isLoadingTracks = false }
        do { tracks = try await api.fetchTracks(search: search) }
        catch { self.error = error.localizedDescription }
    }

    // MARK: Playlists

    func loadPlaylists() async {
        isLoadingPlaylists = true
        defer { isLoadingPlaylists = false }
        do { playlists = try await api.fetchPlaylists() }
        catch { self.error = error.localizedDescription }
    }

    func createPlaylist(name: String) async {
        do {
            let p = try await api.createPlaylist(name: name)
            playlists.append(p)
        } catch { self.error = error.localizedDescription }
    }

    func deletePlaylist(_ playlist: Playlist) async {
        do {
            try await api.deletePlaylist(playlist.id)
            playlists.removeAll { $0.id == playlist.id }
        } catch { self.error = error.localizedDescription }
    }

    // MARK: Drives

    func loadDrives() async {
        do { drives = try await api.fetchDrives() }
        catch { self.error = error.localizedDescription }
    }

    // MARK: WebSocket events

    private func handleWSEvent(_ json: String) {
        guard let data = json.data(using: .utf8),
              let obj  = try? JSONDecoder().decode(RawEvent.self, from: data) else { return }

        switch obj.type {
        case "download_update":
            if let jobData = obj.jobData,
               let job = try? JSONDecoder().decode(DownloadJob.self, from: jobData) {
                if let idx = jobs.firstIndex(where: { $0.jobId == job.jobId }) {
                    jobs[idx] = job
                } else {
                    jobs.insert(job, at: 0)
                }
            }
        case "analysis_update", "export_update":
            break   // handled by polling in the relevant views for now
        default:
            break
        }
    }

    // MARK: Config

    func configure(host: String, port: Int = 5001, token: String) {
        api.config = ServerConfig(host: host, port: port, token: token)
        api.connectWebSocket()
    }
}

// Minimal decodable just to inspect type + extract raw job bytes
private struct RawEvent: Decodable {
    let type: String
    let jobData: Data?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        // Re-encode the "job" sub-object as raw Data for downstream decoding
        if let jobAny = try? c.decodeIfPresent(RawJSON.self, forKey: .job) {
            jobData = jobAny.rawData
        } else {
            jobData = nil
        }
    }
    enum CodingKeys: String, CodingKey { case type, job }
}

private struct RawJSON: Decodable {
    let rawData: Data
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        rawData = try JSONEncoder().encode(c.decode(AnyCodableDict.self))
    }
}

private typealias AnyCodableDict = [String: AnyDecodable]

private struct AnyDecodable: Decodable {
    let value: Any
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let v = try? c.decode(Bool.self)             { value = v }
        else if let v = try? c.decode(Int.self)         { value = v }
        else if let v = try? c.decode(Double.self)      { value = v }
        else if let v = try? c.decode(String.self)      { value = v }
        else if let v = try? c.decode([String: AnyDecodable].self) { value = v }
        else { value = NSNull() }
    }
}

extension AnyDecodable: Encodable {
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch value {
        case let v as Bool:   try c.encode(v)
        case let v as Int:    try c.encode(v)
        case let v as Double: try c.encode(v)
        case let v as String: try c.encode(v)
        default: try c.encodeNil()
        }
    }
}
