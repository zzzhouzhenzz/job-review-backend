#!/usr/bin/env python3
"""Check the iOS build tools. Use --build to compile an unsigned simulator app."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, env=environment, text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))


def inspect(environment: dict[str, str], execute=run) -> dict:
    selected = environment.get("DEVELOPER_DIR")
    if not selected:
        result = execute(["xcode-select", "-p"], environment)
        selected = result.stdout.strip() if result.returncode == 0 else None
    version = execute(["xcodebuild", "-version"], environment)
    sdk = execute(["xcrun", "--sdk", "iphonesimulator", "--show-sdk-path"], environment)
    sdk_version = execute(["xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"], environment)
    xcode_number = re.match(r"Xcode (\d+)(?:\.|\s|$)", version.stdout)
    sdk_number = re.match(r"(\d+)(?:\.|\s|$)", sdk_version.stdout)
    blockers = []
    if version.returncode or not xcode_number:
        blockers.append("Full Xcode is unavailable. Install Xcode from Apple, open it once, then select its developer directory.")
    elif int(xcode_number[1]) < 16:
        blockers.append("This project uses Xcode 16 or later. Select a supported Xcode installation.")
    if sdk.returncode or not sdk.stdout.strip():
        blockers.append("The iOS Simulator SDK is unavailable. Add the iOS platform in Xcode Settings.")
    elif sdk_version.returncode or not sdk_number:
        blockers.append("The iOS Simulator SDK version could not be read.")
    elif int(sdk_number[1]) < 17:
        blockers.append("The app requires an iOS Simulator SDK version of 17 or later.")
    return {
        "developer_directory": selected,
        "xcode_version": version.stdout.strip() if version.returncode == 0 else None,
        "simulator_sdk": sdk.stdout.strip() if sdk.returncode == 0 else None,
        "simulator_sdk_version": sdk_version.stdout.strip() if sdk_version.returncode == 0 else None,
        "ready_for_simulator_build": not blockers,
        "blockers": blockers,
        "diagnostics": {"xcode": version.stderr.strip(), "sdk": sdk.stderr.strip(), "sdk_version": sdk_version.stderr.strip()},
        "limits": [
            "This check does not prove an iOS build or an installed simulator runtime.",
            "A physical iPhone also needs signing, pairing, and Developer Mode.",
            "The current job server is local to this Mac. Remote use needs authenticated HTTPS access.",
        ],
    }


def build_command() -> list[str]:
    return ["xcodebuild", "-project", str(ROOT / "JobReview.xcodeproj"),
            "-scheme", "JobReview", "-configuration", "Debug",
            "-destination", "generic/platform=iOS Simulator",
            "-derivedDataPath", str(ROOT / ".build/iOS"),
            "CODE_SIGNING_ALLOWED=NO", "build"]


def main(argv=None, execute=run, build=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--developer-dir", type=Path,
                        help="Use an installed Xcode developer directory for this command only.")
    parser.add_argument("--json", action="store_true", help="Print the check as JSON.")
    parser.add_argument("--build", action="store_true", help="Build only after the tool check passes.")
    args = parser.parse_args(argv)
    environment = os.environ.copy()
    if args.developer_dir:
        environment["DEVELOPER_DIR"] = str(args.developer_dir.expanduser().resolve())
    report = inspect(environment, execute)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Selected developer directory:", report["developer_directory"] or "Unknown")
        print("iOS build tools:", "Ready" if report["ready_for_simulator_build"] else "Blocked")
        for item in report["blockers"] + report["limits"]:
            print("-", item)
    if not report["ready_for_simulator_build"]:
        return 2
    if args.build:
        if build is None:
            build = subprocess.call
        # Build output goes to the terminal. A successful preflight is not a build receipt.
        return build(build_command(), env=environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
