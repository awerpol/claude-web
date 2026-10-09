import AppKit
import WebKit
import Darwin

@main
struct ClaudeWebApp {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    private let port = AppDelegate.unusedPort() ?? 18765
    private var server: Process?
    private var ownsServer = false
    private var isStopping = false
    private var attempts = 0
    private var window: NSWindow?
    private var webView: WKWebView?
    private var logHandle: FileHandle?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        startServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) { stopServer() }

    private var projectRoot: URL {
        Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
    }

    private var appURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    private func startServer() {
        let python = projectRoot.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            showError("Не найден Python-окружение проекта:\n\(python.path)\n\nСначала настройте проект по README.")
            return
        }

        do {
            let logDirectory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Logs/Claude Web", isDirectory: true)
            try FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)
            let logURL = logDirectory.appendingPathComponent("launcher.log")
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            logHandle = try FileHandle(forWritingTo: logURL)
            try logHandle?.seekToEnd()

            let pipe = Pipe()
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard !data.isEmpty else { return }
                DispatchQueue.main.async { try? self?.logHandle?.write(contentsOf: data) }
            }

            let process = Process()
            process.executableURL = python
            process.arguments = ["-m", "claude_web", "--host", "127.0.0.1", "--port", String(port)]
            process.currentDirectoryURL = projectRoot
            process.environment = ProcessInfo.processInfo.environment
            let extraPaths = [
                FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path,
                "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"
            ]
            process.environment?["PATH"] = (extraPaths + [process.environment?["PATH"] ?? ""]).joined(separator: ":")
            process.standardOutput = pipe
            process.standardError = pipe
            process.terminationHandler = { [weak self] process in
                guard let self, !self.isStopping else { return }
                DispatchQueue.main.async {
                    if self.window == nil {
                        self.showError("Сервер завершился с кодом \(process.terminationStatus).\n\nЖурнал: ~/Library/Logs/Claude Web/launcher.log")
                    }
                }
            }
            try process.run()
            server = process
            ownsServer = true
            waitForServer()
        } catch {
            showError("Не удалось запустить сервер:\n\(error.localizedDescription)")
        }
    }

    private func waitForServer() {
        attempts += 1
        var request = URLRequest(url: appURL)
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                if let response = response as? HTTPURLResponse, response.statusCode == 200 {
                    self.showWindow()
                } else if self.attempts < 40 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForServer() }
                } else {
                    self.showError("Сервер не ответил за 10 секунд.\n\nЖурнал: ~/Library/Logs/Claude Web/launcher.log")
                }
            }
        }.resume()
    }

    private func showWindow() {
        let webView = WKWebView(frame: .zero)
        webView.navigationDelegate = self
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1320, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Claude Web"
        window.minSize = NSSize(width: 850, height: 600)
        window.center()
        window.contentView = webView
        window.delegate = self
        self.window = window
        self.webView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        webView.load(URLRequest(url: appURL))
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Не удалось открыть Claude Web"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Закрыть")
        alert.runModal()
        NSApp.terminate(nil)
    }

    private func stopServer() {
        isStopping = true
        guard ownsServer, let server, server.isRunning else { return }
        server.terminate()
    }

    private static func unusedPort() -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return nil }

        var result = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &result) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &length) }
        }
        guard named == 0 else { return nil }
        return Int(UInt16(bigEndian: result.sin_port))
    }
}
