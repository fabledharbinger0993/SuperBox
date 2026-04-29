import Foundation

// MARK: - Server connection

struct ServerConfig: Codable {
    private static let minPort = 1
    private static let maxPort = 65535

    var host: String        // e.g. "100.94.x.x" (Tailscale) or LAN IP
    var port: Int = 5001
    var token: String

    private var normalizedHost: String? {
        let trimmedHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedHost.isEmpty else { return nil }

        if trimmedHost.hasPrefix("[") && trimmedHost.hasSuffix("]") {
            let start = trimmedHost.index(after: trimmedHost.startIndex)
            let end = trimmedHost.index(before: trimmedHost.endIndex)
            let unbracketedHost = String(trimmedHost[start..<end])
            return unbracketedHost.isEmpty ? nil : unbracketedHost
        }

        return trimmedHost
    }

    var baseURL: URL? {
        guard let normalizedHost, (Self.minPort...Self.maxPort).contains(port) else { return nil }

        var components = URLComponents()
        components.scheme = "http"
        components.host = normalizedHost
        components.port = port
        return components.url
    }
}

// MARK: - Track

struct Track: Identifiable, Codable, Hashable {
    let id: Int
    let title: String?
    let artist: String?
    let album: String?
    let bpm: Double?
    let key: String?
    let duration: Double?
    let filePath: String?
    let dateAdded: String?

    enum CodingKeys: String, CodingKey {
        case id, title, artist, album, bpm, key, duration
        case filePath  = "file_path"
        case dateAdded = "date_added"
    }

    var displayTitle:  String { title  ?? "Unknown Title" }
    var displayArtist: String { artist ?? "Unknown Artist" }
}

// MARK: - Playlist

struct Playlist: Identifiable, Codable, Hashable {
    let id: Int
    let name: String
    let trackCount: Int?
    var tracks: [Track]?

    enum CodingKeys: String, CodingKey {
        case id, name, tracks
        case trackCount = "track_count"
    }
}

// MARK: - Download job

struct DownloadJob: Identifiable, Codable {
    let jobId: String
    let url: String
    let destination: String
    let format: String
    var status: DownloadStatus
    var progress: Int
    var title: String?
    var artist: String?
    var filePath: String?
    var error: String?

    var id: String { jobId }

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case url, destination, format, status, progress, title, artist, error
        case filePath = "file_path"
    }
}

enum DownloadStatus: String, Codable {
    case queued, downloading, converting, importing, done, failed
}

// MARK: - Analysis job

struct AnalysisJob: Codable {
    let  jobId: String
    var  trackIds: [Int]
    var status: String
    var results: [String: AnalysisResult]

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case trackIds = "track_ids"
        case status, results
    }
}

struct AnalysisResult: Codable {
    var status: String?
    var bpm: Double?
    var key: String?
    var error: String?
}

// MARK: - Drive

struct Drive: Identifiable, Codable {
    let name: String
    let path: String
    let pioneer: Bool
    var id: String { path }
}

// MARK: - Export job

struct ExportJob: Codable {
    let jobId: String
    var status: String
    var tracksTotal: Int
    var tracksDone:  Int
    var currentTrack: String?
    var errors: [String]?

    enum CodingKeys: String, CodingKey {
        case jobId       = "job_id"
        case status
        case tracksTotal = "tracks_total"
        case tracksDone  = "tracks_done"
        case currentTrack = "current_track"
        case errors
    }

    /// 0–100 progress derived from track counts.
    var progress: Int {
        guard tracksTotal > 0 else { return status == "complete" ? 100 : 0 }
        return min(100, Int(Double(tracksDone) / Double(tracksTotal) * 100))
    }
}

// MARK: - WebSocket event envelope

struct WSEvent: Decodable {
    let type: String
    let job: AnyCodable?
}

// Simple type-erased Codable wrapper for heterogeneous WS payloads
struct AnyCodable: Codable {
    let value: Any
    init(_ value: Any) { self.value = value }
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let v = try? c.decode(Bool.self)              { value = v; return }
        if let v = try? c.decode(Int.self)               { value = v; return }
        if let v = try? c.decode(Double.self)            { value = v; return }
        if let v = try? c.decode(String.self)            { value = v; return }
        if let v = try? c.decode([AnyCodable].self)      { value = v; return }  // array support
        if let v = try? c.decode([String: AnyCodable].self) { value = v; return }
        value = NSNull()
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch value {
        case let v as Bool:              try c.encode(v)
        case let v as Int:               try c.encode(v)
        case let v as Double:            try c.encode(v)
        case let v as String:            try c.encode(v)
        case let v as [AnyCodable]:      try c.encode(v)
        case let v as [String: AnyCodable]: try c.encode(v)
        default:                         try c.encodeNil()
        }
    }
}
