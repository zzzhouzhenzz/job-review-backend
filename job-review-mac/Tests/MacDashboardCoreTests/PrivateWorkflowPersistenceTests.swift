import Darwin
import Foundation
import ReviewCore
import XCTest
@testable import MacDashboardCore

final class PrivateWorkflowPersistenceTests: XCTestCase {
    private var root: URL!
    private let origin = "https://workflow-fixture.example"

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("workflow-cache-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: root) }

    func testPrivateOriginFilesPreservePendingBytesAcrossRestart() throws {
        let folder = root.appendingPathComponent("private")
        let cache = try PrivateWorkflowPersistence(directory: folder, origin: origin)
        let other = try PrivateWorkflowPersistence(directory: folder, origin: "https://another-fixture.example")
        var pending = try PendingSave(origin: origin, path: "api/v1/decisions", payload: ["request_id": UUID().uuidString], summary: "Fixture")
        // Preserve unusual but valid JSON spacing and key order rather than re-encoding a retry.
        pending.body = Data("{ \"request_id\": \"fixed-fixture-id\", \"note\" : \"Original bytes\" }\n".utf8)
        try cache.save(SavedWorkflowSession(origin: origin, pending: pending))
        XCTAssertNil(try other.load())
        XCTAssertNotEqual(cache.file, other.file)
        XCTAssertFalse(cache.file.lastPathComponent.contains("example"))
        let restored = try PrivateWorkflowPersistence(directory: folder, origin: origin).load()
        XCTAssertEqual(restored?.pending, pending)
        XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: folder.path)[.posixPermissions] as? Int, 0o700)
        XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: cache.file.path)[.posixPermissions] as? Int, 0o600)
        XCTAssertFalse(String(decoding: try Data(contentsOf: cache.file), as: UTF8.self).contains("\"token\""))
    }

    func testAtomicReplacementLeavesNoTemporaryFiles() throws {
        let cache = try PrivateWorkflowPersistence(directory: root, origin: origin)
        try cache.save(SavedWorkflowSession(origin: origin, savedAt: Date(timeIntervalSince1970: 1)))
        let first = try FileManager.default.attributesOfItem(atPath: cache.file.path)[.systemFileNumber] as? UInt64
        try cache.save(SavedWorkflowSession(origin: origin, savedAt: Date(timeIntervalSince1970: 2)))
        let second = try FileManager.default.attributesOfItem(atPath: cache.file.path)[.systemFileNumber] as? UInt64
        XCTAssertNotEqual(first, second)
        XCTAssertEqual(try cache.load()?.savedAt, Date(timeIntervalSince1970: 2))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [cache.file.lastPathComponent])
    }

    func testRejectsSymlinkDirectoryAndFileWithoutReplacingTargets() throws {
        let destination = root.appendingPathComponent("actual")
        let cache = try PrivateWorkflowPersistence(directory: destination, origin: origin)
        try cache.save(SavedWorkflowSession(origin: origin))
        let link = root.appendingPathComponent("link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: destination)
        XCTAssertThrowsError(try PrivateWorkflowPersistence(directory: link, origin: origin))
        let target = root.appendingPathComponent("keep.txt")
        try Data("Keep this target".utf8).write(to: target)
        try FileManager.default.removeItem(at: cache.file)
        try FileManager.default.createSymbolicLink(at: cache.file, withDestinationURL: target)
        XCTAssertThrowsError(try cache.load())
        XCTAssertThrowsError(try cache.save(SavedWorkflowSession(origin: origin)))
        XCTAssertThrowsError(try PrivateWorkflowPersistence(directory: destination, origin: origin))
        XCTAssertEqual(try String(contentsOf: target), "Keep this target")
    }

    func testRejectsInsecureDirectoryAndFileModes() throws {
        let cache = try PrivateWorkflowPersistence(directory: root, origin: origin)
        try cache.save(SavedWorkflowSession(origin: origin))
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: cache.file.path)
        XCTAssertThrowsError(try cache.load())
        XCTAssertThrowsError(try cache.save(SavedWorkflowSession(origin: origin)))
        XCTAssertThrowsError(try PrivateWorkflowPersistence(directory: root, origin: origin))
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: cache.file.path)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: root.path)
        XCTAssertThrowsError(try cache.load())
        XCTAssertThrowsError(try cache.save(SavedWorkflowSession(origin: origin)))
        XCTAssertThrowsError(try PrivateWorkflowPersistence(directory: root, origin: origin))
    }

    func testRejectsForeignOriginsAndMalformedFilesWithoutLeakingContents() throws {
        let cache = try PrivateWorkflowPersistence(directory: root, origin: origin)
        XCTAssertThrowsError(try cache.save(SavedWorkflowSession(origin: "https://another-fixture.example")))
        let pending = try PendingSave(origin: "https://another-fixture.example", path: "api/v1/decisions", payload: ["request_id": "fixture"], summary: "Fixture")
        XCTAssertThrowsError(try cache.save(SavedWorkflowSession(origin: origin, pending: pending)))
        try cache.save(SavedWorkflowSession(origin: origin))
        try Data("PRIVATE-FIXTURE-CONTENTS".utf8).write(to: cache.file)
        XCTAssertThrowsError(try cache.load()) { error in
            XCTAssertFalse(error.localizedDescription.contains("PRIVATE-FIXTURE-CONTENTS"))
            XCTAssertFalse(error.localizedDescription.contains(self.root.path))
        }
    }

    func testRejectsHardLinkedJournal() throws {
        let cache = try PrivateWorkflowPersistence(directory: root, origin: origin)
        try cache.save(SavedWorkflowSession(origin: origin))
        try FileManager.default.linkItem(at: cache.file, to: root.appendingPathComponent("other-name"))
        XCTAssertThrowsError(try cache.load())
        XCTAssertThrowsError(try cache.save(SavedWorkflowSession(origin: origin)))
    }
}
