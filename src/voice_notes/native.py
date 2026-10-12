"""Build the bundled macOS helper using the installed Swift SDK."""
import os
import platform
import subprocess
from pathlib import Path


def build_helper(build_dir: Path) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("Audio capture requires macOS 26 or newer")
    if int(platform.mac_ver()[0].split(".")[0]) < 26:
        raise RuntimeError("This helper requires macOS 26+. See docs/research.md for older macOS options.")
    source_dir = Path(__file__).parent / "native"
    source, plist = source_dir / "Capture.swift", source_dir / "Info.plist"
    binary = build_dir / "voice-notes-capture"
    if binary.exists() and binary.stat().st_mtime >= max(source.stat().st_mtime, plist.stat().st_mtime):
        return binary.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    temporary = binary.with_suffix(".tmp")
    command = ["xcrun", "swiftc", "-parse-as-library", "-swift-version", "5", "-O",
               "-module-cache-path", str(build_dir / "module-cache"), str(source),
               "-o", str(temporary), "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
               "-Xlinker", "__info_plist", "-Xlinker", str(plist)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Swift helper build failed. Install Xcode/Command Line Tools with a macOS 26 SDK.\n" + result.stderr)
    # Stable ad-hoc signing identity helps macOS associate permissions with the helper.
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier",
                    "local.voice-notes.capture", str(temporary)], check=True, capture_output=True)
    os.replace(temporary, binary)
    return binary.resolve()
