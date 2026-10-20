#!/usr/bin/env python3
"""Build a local, ad-hoc signed notification helper; no paid signing identity."""
import argparse
import platform
import plistlib
import subprocess
from pathlib import Path

BUNDLE_ID = "com.sueun.ai-news-radar.notifier"
MIN_MACOS = "13.0"
ARCHITECTURES = ("arm64", "x86_64")


def _xcrun(*args):
    return subprocess.run(["xcrun", *args], capture_output=True, text=True, check=True).stdout.strip()


def build(output, port=8741, data_dir=None):
    """Compile ``native/main.swift`` into ``output`` (an .app bundle) bound to the collector's port."""
    if platform.system() != "Darwin":
        raise RuntimeError("The notification helper requires macOS.")
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid local port")
    compiler = _xcrun("--find", "swiftc")
    sdk = _xcrun("--sdk", "macosx", "--show-sdk-path")
    arch = platform.machine()
    if arch not in ARCHITECTURES:
        raise RuntimeError("Unsupported Mac architecture: " + arch)
    contents = Path(output) / "Contents"
    binary = contents / "MacOS" / "AIRadar"
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([compiler, str(Path(__file__).parent / "native/main.swift"), "-O", "-sdk", sdk,
                    "-target", arch + "-apple-macos" + MIN_MACOS, "-framework", "AppKit",
                    "-framework", "UserNotifications", "-o", str(binary)], check=True)
    info = dict(CFBundleDisplayName="AI News Radar", CFBundleName="AI News Radar", CFBundleExecutable="AIRadar",
                CFBundleIdentifier=BUNDLE_ID, CFBundlePackageType="APPL", CFBundleShortVersionString="1.0.0",
                CFBundleVersion="1", LSMinimumSystemVersion=MIN_MACOS, LSUIElement=True, NSHighResolutionCapable=True,
                AIRadarBaseURL="http://127.0.0.1:" + str(port))
    if data_dir:
        info["AIRadarDataDirectory"] = str(Path(data_dir).resolve())
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--sign", "-", str(output)], check=True, capture_output=True)
    subprocess.run(["codesign", "--verify", "--strict", str(output)], check=True, capture_output=True)
    return binary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/AI News Radar.app"))
    parser.add_argument("--port", type=int, default=8741)
    args = parser.parse_args()
    print(build(args.output, args.port))
