import Darwin
import Foundation
import XCTest
@testable import MacDashboardCore

final class ConnectionSettingsTests: XCTestCase {
    private var directory: URL!
    private let credential = String(repeating: "T", count: 43)

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
    }

    override func tearDownWithError() throws { try FileManager.default.removeItem(at: directory) }

    private func write(_ text: String, to url: URL, mode: Int = 0o600) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
        try text.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: url.path)
    }

    func testOriginValidationAndNormalization() throws {
        let settings = try ConnectionSettings(origin: "HTTPS://Fixture.Example:443/", credentialFile: "/private/test")
        XCTAssertEqual(settings.origin, "https://fixture.example")
        for origin in ["http://fixture.example", "http://127.0.0.1:8766", "https://user@fixture.example",
                       "https://fixture.example/path", "https://fixture.example?token=secret", "https://fixture.example#part",
                       "file:///tmp/test", "https://fixture.example:0", "https://fixture.example:65536"] {
            XCTAssertThrowsError(try ConnectionSettings(origin: origin, credentialFile: "/private/test"))
        }
        XCTAssertThrowsError(try ConnectionSettings(origin: "https://fixture.example", credentialFile: "relative.txt"))
        #if DEBUG
        XCTAssertEqual(try ConnectionSettings(origin: "http://127.0.0.1:8766", credentialFile: "/private/test",
                                              allowHTTPForTesting: true).origin, "http://127.0.0.1:8766")
        XCTAssertThrowsError(try ConnectionSettings(origin: "http://example.com", credentialFile: "/private/test",
                                                    allowHTTPForTesting: true))
        #else
        XCTAssertThrowsError(try ConnectionSettings(origin: "http://127.0.0.1:8766", credentialFile: "/private/test",
                                                    allowHTTPForTesting: true))
        #endif
    }

    func testPrivateCredentialRejectsPublicModesSymlinksAndDirectories() throws {
        let file = directory.appendingPathComponent("credential.txt")
        try write(credential + "\n", to: file)
        let settings = try ConnectionSettings(origin: "https://fixture.example", credentialFile: file.path)
        XCTAssertEqual(try settings.readCredential(), credential)
        for mode in [0o644, 0o640, 0o400, 0o700] {
            try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: file.path)
            XCTAssertThrowsError(try settings.readCredential())
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        let link = directory.appendingPathComponent("credential-link.txt")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: file)
        XCTAssertThrowsError(try ConnectionSettings(origin: "https://fixture.example", credentialFile: link.path).readCredential())
        XCTAssertThrowsError(try ConnectionSettings(origin: "https://fixture.example", credentialFile: directory.path).readCredential())
    }

    func testCredentialFormatAndSizeLimits() throws {
        let file = directory.appendingPathComponent("credential.txt")
        let settings = try ConnectionSettings(origin: "https://fixture.example", credentialFile: file.path)
        for value in ["", "short", credential + "x", "token\n" + credential, String(repeating: "a", count: 2_000)] {
            try write(value, to: file)
            XCTAssertThrowsError(try settings.readCredential())
        }
    }

    func testExplicitConfigurationWinsAndContainsOnlyFileReference() throws {
        let credentialFile = directory.appendingPathComponent("private-credential.txt")
        try write(credential, to: credentialFile)
        let config = directory.appendingPathComponent(".config/job-review-mac/connection.json")
        let object = ["origin": "https://explicit.example", "credential_file": credentialFile.path]
        try write(String(decoding: JSONSerialization.data(withJSONObject: object), as: UTF8.self), to: config)
        let settings = try ConnectionSettings.loadDefault(homeDirectory: directory, executableURL: nil, currentDirectory: directory)
        XCTAssertEqual(settings.origin, "https://explicit.example")
        XCTAssertEqual(settings.credentialFile, credentialFile.path)
        XCTAssertFalse(String(reflecting: settings).contains(credential))
    }

    func testAutoDiscoveryFromExecutableAncestor() throws {
        let workspace = directory.appendingPathComponent("workspace")
        let device = workspace.appendingPathComponent("job-review/state/device")
        try write("{\"schema_version\":1,\"public_origin\":\"https://discovered.example\",\"token_sha256\":\"fictional-hash\"}",
                  to: device.appendingPathComponent("tailscale-access.json"))
        try write(credential, to: device.appendingPathComponent("tailscale-credential.txt"))
        let binary = workspace.appendingPathComponent("job-review-mac/.build/Job Review.app/Contents/MacOS/JobReviewMac")
        let settings = try ConnectionSettings.loadDefault(homeDirectory: directory, executableURL: binary,
                                                          currentDirectory: directory.appendingPathComponent("unrelated"))
        XCTAssertEqual(settings.origin, "https://discovered.example")
        XCTAssertEqual(settings.credentialFile, device.appendingPathComponent("tailscale-credential.txt").path)
    }

    func testInvalidExplicitConfigurationDoesNotFallBack() throws {
        let device = directory.appendingPathComponent("job-review/state/device")
        try write("{\"schema_version\":1,\"public_origin\":\"https://fallback.example\"}", to: device.appendingPathComponent("tailscale-access.json"))
        try write(credential, to: device.appendingPathComponent("tailscale-credential.txt"))
        let config = directory.appendingPathComponent(".config/job-review-mac/connection.json")
        try write("invalid", to: config)
        XCTAssertThrowsError(try ConnectionSettings.loadDefault(homeDirectory: directory, executableURL: nil,
                                                                currentDirectory: directory))
        try write("{}", to: config, mode: 0o644)
        XCTAssertThrowsError(try ConnectionSettings.loadDefault(homeDirectory: directory, executableURL: nil,
                                                                currentDirectory: directory))
    }

    func testMissingConfigurationIsActionableAndDoesNotExposePaths() {
        XCTAssertThrowsError(try ConnectionSettings.loadDefault(homeDirectory: directory, executableURL: nil,
                                                                currentDirectory: directory)) { error in
            XCTAssertEqual(error as? DashboardError, .configuration)
            XCTAssertFalse(error.localizedDescription.contains(self.directory.path))
            XCTAssertNotNil((error as? DashboardError)?.recoverySuggestion)
        }
    }
}
