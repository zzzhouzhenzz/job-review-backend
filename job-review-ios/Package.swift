// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JobReview",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "ReviewCore", targets: ["ReviewCore"]),
        .executable(name: "JobReviewPreview", targets: ["JobReviewPreview"]),
        .executable(name: "ReviewCoreChecks", targets: ["ReviewCoreChecks"]),
        .executable(name: "ReviewHTTPSChecks", targets: ["ReviewHTTPSChecks"])
    ],
    targets: [
        .target(name: "ReviewCore", resources: [.process("Resources")]),
        // A macOS build checks shared SwiftUI source when an iOS SDK is unavailable.
        .executableTarget(name: "JobReviewPreview", dependencies: ["ReviewCore"], path: "App", exclude: ["Info.plist", "Assets.xcassets"], resources: [.copy("Fonts")]),
        .executableTarget(name: "ReviewCoreChecks", dependencies: ["ReviewCore"], path: "Tests/ReviewCoreChecks"),
        .testTarget(name: "WorkflowConfirmationTests", dependencies: ["JobReviewPreview", "ReviewCore"], path: "Tests/WorkflowConfirmationTests"),
        .executableTarget(name: "ReviewHTTPSChecks", dependencies: ["ReviewCore"], path: "Tests/ReviewHTTPSChecks")
    ]
)
