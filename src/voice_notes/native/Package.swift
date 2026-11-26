// swift-tools-version: 6.0
import PackageDescription

// The capture helper is built by voice_notes/native.py, which also embeds
// Info.plist and signs the binary. FluidAudio is pinned exactly: its CoreML
// model repositories and streaming APIs change between minor releases.
let package = Package(
    name: "voice-notes-capture",
    platforms: [.macOS("26.0")],
    dependencies: [
        .package(url: "https://github.com/FluidInference/FluidAudio.git", exact: "0.18.4"),
    ],
    targets: [
        .executableTarget(
            name: "voice-notes-capture",
            dependencies: [.product(name: "FluidAudio", package: "FluidAudio")],
            path: "Sources/voice-notes-capture",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
