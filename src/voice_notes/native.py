"""Build the bundled macOS helper using the installed Swift SDK."""
import os
import platform
import subprocess
import tempfile
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
    # Independent temporary paths allow simultaneous CLI invocations to build
    # without clobbering one another or replacing a working helper on failure.
    with tempfile.TemporaryDirectory(prefix=".compile-", dir=build_dir) as directory:
        temporary = Path(directory) / binary.name
        command = ["xcrun", "swiftc", "-parse-as-library", "-swift-version", "5", "-O",
                   "-module-cache-path", str(build_dir / "module-cache"), str(source),
                   "-o", str(temporary), "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
                   "-Xlinker", "__info_plist", "-Xlinker", str(plist)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise RuntimeError("Swift helper build failed. Install Xcode/Command Line Tools with a macOS 26 SDK.\n" + result.stderr)
            subprocess.run(["codesign", "--force", "--sign", "-", "--identifier",
                            "local.voice-notes.capture", str(temporary)],
                           check=True, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Native helper build/signing timed out; the previous helper was preserved. "
                               "Check your Xcode installation and retry voice-notes build.") from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError("Could not sign the capture helper; the previous helper was preserved.") from error
        os.replace(temporary, binary)
    return binary.resolve()
