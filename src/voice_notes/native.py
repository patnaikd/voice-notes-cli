"""Build the bundled macOS helper (a SwiftPM package) using the installed Swift SDK."""
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

# The first build fetches FluidAudio and compiles it; later builds are incremental.
BUILD_TIMEOUT = 900
SIGN_TIMEOUT = 30


def _newest_source(package: Path) -> float:
    files = [package / "Package.swift", package / "Info.plist", package / "Package.resolved",
             *(package / "Sources").rglob("*.swift")]
    return max(path.stat().st_mtime for path in files if path.exists())


def build_helper(build_dir: Path) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("Audio capture requires macOS 26 or newer")
    if int(platform.mac_ver()[0].split(".")[0]) < 26:
        raise RuntimeError("This helper requires macOS 26+. See docs/research.md for older macOS options.")
    package = Path(__file__).parent / "native"
    plist = package / "Info.plist"
    binary = build_dir / "voice-notes-capture"
    if binary.exists() and binary.stat().st_mtime >= _newest_source(package):
        return binary.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    scratch = (build_dir / "swiftpm").resolve()
    # Independent temporary paths allow simultaneous CLI invocations to build
    # without replacing a working helper when a build fails.
    with tempfile.TemporaryDirectory(prefix=".compile-", dir=build_dir) as directory:
        temporary = Path(directory) / binary.name
        command = ["xcrun", "swift", "build", "-c", "release", "--package-path", str(package),
                   "--scratch-path", str(scratch), "--product", "voice-notes-capture",
                   "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
                   "-Xlinker", "__info_plist", "-Xlinker", str(plist.resolve())]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
            if result.returncode:
                raise RuntimeError("Swift helper build failed. Install Xcode/Command Line Tools with a macOS 26 SDK "
                                   "and check network access (the first build downloads FluidAudio).\n"
                                   + result.stderr[-4000:])
            built = scratch / "release" / "voice-notes-capture"
            if not built.is_file():
                raise RuntimeError(f"Swift build finished but produced no helper at {built}")
            shutil.copy2(built, temporary)
            subprocess.run(["codesign", "--force", "--sign", "-", "--identifier",
                            "local.voice-notes.capture", str(temporary)],
                           check=True, capture_output=True, timeout=SIGN_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Native helper build/signing timed out; the previous helper was preserved. "
                               "Check your Xcode installation and retry voice-notes build.") from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError("Could not sign the capture helper; the previous helper was preserved.") from error
        os.replace(temporary, binary)
    return binary.resolve()
