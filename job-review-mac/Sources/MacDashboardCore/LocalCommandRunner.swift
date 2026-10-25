import Foundation
import Darwin

struct LocalCommandResult: Sendable { let code: Int32; let output: String }
protocol LocalCommandRunning: Sendable {
    func run(_ executable: String, _ arguments: [String], timeout: TimeInterval) async throws -> LocalCommandResult
}

/// Fixed helper programs run off the UI thread. Output is drained concurrently and kept bounded.
struct LocalCommandRunner: LocalCommandRunning {
    func run(_ executable: String, _ arguments: [String], timeout: TimeInterval) async throws -> LocalCommandResult {
        try await Task.detached {
            guard ["/bin/launchctl", "/bin/ps", "/usr/sbin/lsof"].contains(executable) else {
                throw LocalServerError("Unsupported local service helper.")
            }
            let process = Process(), pipe = Pipe(), finished = DispatchSemaphore(value: 0)
            process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = pipe; process.standardError = pipe
            process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"]
            process.terminationHandler = { _ in finished.signal() }
            do { try process.run() } catch { throw LocalServerError("Could not run the local service helper: \(error.localizedDescription)") }
            let capture = LocalCommandCapture(), drained = DispatchSemaphore(value: 0)
            DispatchQueue.global(qos: .utility).async {
                while true {
                    let data = pipe.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    capture.append(data)
                }
                drained.signal()
            }
            if finished.wait(timeout: .now() + timeout) == .timedOut {
                process.terminate()
                if finished.wait(timeout: .now() + 1) == .timedOut {
                    // Terminate only our hung helper. Never signal the backend directly.
                    kill(process.processIdentifier, SIGKILL)
                }
                throw LocalServerError("The local service command timed out. Its outcome is unknown; refresh status.")
            }
            guard drained.wait(timeout: .now() + 2) == .success, let text = capture.text else {
                throw LocalServerError("The local service helper returned incomplete or excessive output.")
            }
            return LocalCommandResult(code: process.terminationStatus, output: text)
        }.value
    }
}

private final class LocalCommandCapture: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private var overflow = false
    func append(_ chunk: Data) {
        lock.lock(); defer { lock.unlock() }
        if data.count + chunk.count > 8_000_000 { overflow = true }
        else if !overflow { data.append(chunk) }
    }
    var text: String? {
        lock.lock(); defer { lock.unlock() }
        return overflow ? nil : String(data: data, encoding: .utf8)
    }
}
