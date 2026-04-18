import Foundation

// MARK: - API errors

enum APIError: LocalizedError {
    case notConfigured
    case httpError(Int)
    case decodingError(Error)
    case networkError(Error)
    case message(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured:        return "No server configured. Add your RekitBox address in Settings."
        case .httpError(let code):  return "Server returned \(code)."
        case .decodingError(let e): return "Response parse failed: \(e.localizedDescription)"
        case .networkError(let e):  return e.localizedDescription
        case .message(let m):       return m
        }
    }
}

// MARK: - API client

@MainActor
final class APIClient: ObservableObject {

    static let shared = APIClient()

    @Published var config: ServerConfig? {
        didSet { saveConfig() }
    }

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest  = 30
        cfg.timeoutIntervalForResource = 30
        return URLSession(configuration: cfg)
    }()
    private var wsTask: URLSessionWebSocketTask?
    private var wsRetryCount = 0
    var onEvent: ((String) -> Void)?   // raw JSON string → Store parses it

    init() { config = loadConfig() }

    // MARK: Connection check

    func ping() async throws {
        let data = try await get("/api/mobile/ping")
        _ = try decode([String: String].self, from: data)
    }

    // MARK: Downloads

    func enqueueDownload(url: String, destination: String,
                         format: String = "aiff", filename: String? = nil) async throws -> String {
        var body: [String: String] = ["url": url, "destination": destination, "format": format]
        if let fn = filename { body["filename"] = fn }
        let data = try await post("/api/mobile/download", body: body)
        let resp = try decode([String: String].self, from: data)
        guard let jobId = resp["job_id"] else { throw APIError.message("No job_id in response") }
        return jobId
    }

    func fetchJobs() async throws -> [DownloadJob] {
        let data = try await get("/api/mobile/jobs")
        return try decode([DownloadJob].self, from: data)
    }

    // MARK: Tracks

    func fetchTracks(search: String? = nil, sort: String = "date_added",
                     limit: Int = 200, offset: Int = 0) async throws -> [Track] {
        var q = "sort=\(sort)&limit=\(limit)&offset=\(offset)"
        if let s = search, !s.isEmpty { q += "&search=\(s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s)" }
        let data = try await get("/api/mobile/rekordbox/tracks?\(q)")
        return try decode([Track].self, from: data)
    }

    // MARK: Playlists

    func fetchPlaylists() async throws -> [Playlist] {
        let data = try await get("/api/mobile/rekordbox/playlists")
        return try decode([Playlist].self, from: data)
    }

    func fetchPlaylist(_ id: Int) async throws -> Playlist {
        let data = try await get("/api/mobile/rekordbox/playlists/\(id)")
        return try decode(Playlist.self, from: data)
    }

    func createPlaylist(name: String) async throws -> Playlist {
        let data = try await post("/api/mobile/rekordbox/playlists", body: ["name": name])
        return try decode(Playlist.self, from: data)
    }

    func renamePlaylist(_ id: Int, name: String) async throws {
        _ = try await put("/api/mobile/rekordbox/playlists/\(id)", body: ["name": name])
    }

    func deletePlaylist(_ id: Int) async throws {
        _ = try await delete("/api/mobile/rekordbox/playlists/\(id)")
    }

    func addTrackToPlaylist(playlistId: Int, trackId: Int) async throws {
        _ = try await post("/api/mobile/rekordbox/playlists/\(playlistId)/tracks",
                           body: ["track_id": trackId])
    }

    func removeTrackFromPlaylist(playlistId: Int, trackId: Int) async throws {
        _ = try await delete("/api/mobile/rekordbox/playlists/\(playlistId)/tracks/\(trackId)")
    }

    // MARK: Analysis

    func analyzeTrack(_ trackId: Int) async throws -> String {
        let data = try await post("/api/mobile/rekordbox/analyze", body: ["track_ids": [trackId]])
        let resp = try decode([String: String].self, from: data)
        guard let jobId = resp["job_id"] else { throw APIError.message("No job_id") }
        return jobId
    }

    func fetchAnalysisJob(_ jobId: String) async throws -> AnalysisJob {
        let data = try await get("/api/mobile/rekordbox/analyze/\(jobId)")
        return try decode(AnalysisJob.self, from: data)
    }

    // MARK: Drives & Export

    func fetchDrives() async throws -> [Drive] {
        let data = try await get("/api/mobile/drives")
        return try decode([Drive].self, from: data)
    }

    func startExport(playlistIds: [Int], drivePath: String) async throws -> String {
        let body = ExportRequest(playlistIds: playlistIds, drivePath: drivePath)
        let data = try await post("/api/mobile/export", body: body)
        let resp = try decode([String: String].self, from: data)
        guard let jobId = resp["job_id"] else { throw APIError.message("No job_id") }
        return jobId
    }

    func fetchExportJob(_ jobId: String) async throws -> ExportJob {
        let data = try await get("/api/mobile/export/\(jobId)")
        return try decode(ExportJob.self, from: data)
    }

    // MARK: Folders

    func fetchFolders() async throws -> [[String: Any]] {
        let data = try await get("/api/mobile/folders")
        guard let arr = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return arr
    }

    // MARK: WebSocket

    func connectWebSocket() {
        guard let cfg = config else { return }
        guard let url = URL(string: "ws://\(cfg.host):\(cfg.port)/api/mobile/events") else {
            print("[RekitGo] connectWebSocket: invalid URL for host \(cfg.host)")
            return
        }
        disconnectWebSocket()
        var req = URLRequest(url: url)
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        wsTask = URLSession.shared.webSocketTask(with: req)
        wsTask?.resume()
        wsRetryCount = 0
        receiveNextMessage()
    }

    func disconnectWebSocket() {
        wsTask?.cancel(with: .normalClosure, reason: nil)
        wsTask = nil
    }

    private func scheduleReconnect() {
        let delay = min(pow(2.0, Double(wsRetryCount)), 60.0)
        wsRetryCount = min(wsRetryCount + 1, 6)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.connectWebSocket()
        }
    }

    private func receiveNextMessage() {
        wsTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(.string(let text)):
                DispatchQueue.main.async {
                    self.wsRetryCount = 0   // reset backoff on successful receive
                    self.onEvent?(text)
                }
                self.receiveNextMessage()
            case .success(.data(let d)):
                if let text = String(data: d, encoding: .utf8) {
                    DispatchQueue.main.async {
                        self.wsRetryCount = 0
                        self.onEvent?(text)
                    }
                }
                self.receiveNextMessage()
            case .failure:
                DispatchQueue.main.async { self.scheduleReconnect() }
            @unknown default: break
            }
        }
    }

    // MARK: HTTP primitives

    private func request(_ method: String, _ path: String, body: Data? = nil) async throws -> Data {
        guard let cfg = config else { throw APIError.notConfigured }
        guard let base = cfg.baseURL else { throw APIError.notConfigured }
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = method
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        if let b = body {
            req.httpBody = b
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                throw APIError.httpError(http.statusCode)
            }
            return data
        } catch let e as APIError { throw e }
        catch { throw APIError.networkError(error) }
    }

    private func get(_ path: String) async throws -> Data {
        try await request("GET", path)
    }

    private func post<B: Encodable>(_ path: String, body: B) async throws -> Data {
        try await request("POST", path, body: try JSONEncoder().encode(body))
    }

    private func put<B: Encodable>(_ path: String, body: B) async throws -> Data {
        try await request("PUT", path, body: try JSONEncoder().encode(body))
    }

    private func delete(_ path: String) async throws -> Data {
        try await request("DELETE", path)
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do { return try JSONDecoder().decode(type, from: data) }
        catch { throw APIError.decodingError(error) }
    }

    // MARK: Config persistence

    private func saveConfig() {
        guard let cfg = config else { return }
        do {
            let data = try JSONEncoder().encode(cfg)
            UserDefaults.standard.set(data, forKey: "serverConfig")
        } catch {
            print("[RekitGo] saveConfig failed: \(error)")
        }
    }

    private func loadConfig() -> ServerConfig? {
        guard let data = UserDefaults.standard.data(forKey: "serverConfig") else { return nil }
        do {
            return try JSONDecoder().decode(ServerConfig.self, from: data)
        } catch {
            print("[RekitGo] loadConfig failed: \(error)")
            return nil
        }
    }
}

// MARK: - Request body types

private struct ExportRequest: Encodable {
    let playlistIds: [Int]
    let drivePath: String
    enum CodingKeys: String, CodingKey {
        case playlistIds = "playlist_ids"
        case drivePath   = "drive_path"
    }
}
