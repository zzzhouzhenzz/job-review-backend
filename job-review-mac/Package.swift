// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JobReviewMac",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "JobReviewMac", targets: ["JobReviewMac"]), .executable(name: "DashboardProbe", targets: ["DashboardProbe"])],
    dependencies: [.package(path: "../job-review-ios")],
    targets: [
        .target(name: "MacDashboardCore", dependencies: [.product(name: "ReviewCore", package: "job-review-ios")]),
        .executableTarget(name: "JobReviewMac", dependencies: ["MacDashboardCore", .product(name: "ReviewCore", package: "job-review-ios")]),
        .executableTarget(name: "DashboardProbe", dependencies: ["MacDashboardCore", .product(name: "ReviewCore", package: "job-review-ios")]),
        .testTarget(name: "MacDashboardCoreTests", dependencies: ["MacDashboardCore", .product(name: "ReviewCore", package: "job-review-ios")], resources: [.copy("Fixtures")]),
        .testTarget(name: "JobReviewMacTests", dependencies: ["JobReviewMac", "MacDashboardCore", .product(name: "ReviewCore", package: "job-review-ios")])
    ]
)
