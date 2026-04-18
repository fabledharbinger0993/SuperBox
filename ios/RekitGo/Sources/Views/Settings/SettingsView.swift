import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: AppStore
    var isInitialSetup: Bool

    @State private var host  = ""
    @State private var token = ""
    @State private var port  = "5001"
    @State private var status: String? = nil
    @State private var testing = false

    var body: some View {
        NavigationStack {
            Form {
                if isInitialSetup {
                    Section {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Welcome to RekitGo").font(.headline)
                            Text("Enter your RekitBox server address and the token shown in the 📱 pairing panel.")
                                .font(.caption).foregroundStyle(.secondary)
                        }.padding(.vertical, 4)
                    }
                }

                Section("Server") {
                    TextField("IP or Tailscale address", text: $host)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                    TextField("Port", text: $port)
                        .keyboardType(.numberPad)
                }

                Section("Auth Token") {
                    SecureField("Paste token from RekitBox pairing panel", text: $token)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                }

                Section {
                    Button(action: testAndSave) {
                        HStack {
                            if testing {
                                ProgressView().padding(.trailing, 4)
                            }
                            Text(isInitialSetup ? "Connect" : "Save & Test")
                        }
                    }
                    .disabled(host.isEmpty || token.isEmpty || testing)

                    if let s = status {
                        Text(s)
                            .font(.caption)
                            .foregroundStyle(s.hasPrefix("✓") ? .green : .red)
                    }
                }

                if !isInitialSetup, let cfg = store.api.config {
                    Section("Current") {
                        LabeledContent("Host", value: cfg.host)
                        LabeledContent("Port", value: "\(cfg.port)")
                    }
                }
            }
            .navigationTitle(isInitialSetup ? "Set Up RekitGo" : "Settings")
            .onAppear {
                if let cfg = store.api.config {
                    host  = cfg.host
                    port  = "\(cfg.port)"
                    token = cfg.token
                }
            }
        }
    }

    private func testAndSave() {
        testing = true
        status  = nil
        let h = host.trimmingCharacters(in: .whitespaces)
        let t = token.trimmingCharacters(in: .whitespaces)
        let p = Int(port) ?? 5001
        store.configure(host: h, port: p, token: t)
        Task {
            do {
                try await store.api.ping()
                status  = "✓ Connected to RekitBox"
            } catch {
                status  = "✗ \(error.localizedDescription)"
                store.api.config = nil
            }
            testing = false
        }
    }
}
