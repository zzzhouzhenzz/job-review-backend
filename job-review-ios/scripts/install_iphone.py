#!/usr/bin/env python3
"""List/check physical iPhones; build, install, and launch only with --device ID --install."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "app.jobreview.personal"


class InstallError(Exception):
    def __init__(self, message: str, returncode: int = 2):
        super().__init__(message)
        self.returncode = returncode if returncode > 0 else 1


@dataclass(frozen=True)
class Device:
    identifier: str
    udid: str | None
    name: str | None
    device_type: str | None
    platform: str | None
    reality: str | None
    pairing: str | None
    connection: str | None

    def blockers(self, *, require_connection: bool = True) -> list[str]:
        reasons = []
        if self.reality != "physical":
            reasons.append(f"reality is {self.reality or 'unknown'}, not physical")
        if self.device_type != "iPhone" or self.platform != "iOS":
            reasons.append("device is not a confirmed iOS iPhone")
        if not self.udid or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.udid):
            reasons.append("hardware UDID is missing or invalid")
        if self.pairing != "paired":
            reasons.append("device is not paired; unlock it and trust this Mac")
        if require_connection and self.connection != "connected":
            reasons.append("device is not connected; connect it and open Xcode")
        return reasons


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise InstallError(f"Invalid devicectl JSON: {label} must be an object.")
    return value


def _text(mapping: dict, key: str) -> str | None:
    value = mapping.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()
                              or any(ord(char) < 32 for char in value)):
        raise InstallError(f"Invalid devicectl JSON: {key} must be a nonempty string.")
    return value


def parse_device(row) -> Device:
    row = _object(row, "device")
    identifier = _text(row, "identifier")
    if not identifier or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", identifier):
        raise InstallError("Invalid devicectl JSON: device identifier is missing or invalid.")
    properties = _object(row.get("properties"), "device.properties")
    hardware = _object(properties.get("hardware"), "properties.hardware")
    connection = _object(properties.get("connection"), "properties.connection")
    state = _object(properties.get("state"), "properties.state")
    return Device(
        identifier, _text(hardware, "udid"), _text(state, "name"),
        _text(hardware, "deviceType"), _text(hardware, "platform"),
        _text(hardware, "reality"), _text(connection, "pairingState"),
        _text(connection, "state"),
    )


def parse_devices(document) -> list[Device]:
    document = _object(document, "document")
    info = _object(document.get("info"), "info")
    if info.get("outcome") != "success":
        raise InstallError("devicectl did not report a successful device listing.")
    rows = _object(document.get("result"), "result").get("devices")
    if not isinstance(rows, list):
        raise InstallError("Invalid devicectl JSON: result.devices must be an array.")
    return [parse_device(row) for row in rows]


def run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(command, timeout=timeout, check=False)


def checked(stage: str, command: list[str], execute=run, *, timeout: int = 120) -> None:
    print(f"{stage}...", flush=True)
    try:
        result = execute(command, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"{stage} failed: {error}") from error
    if result.returncode != 0:
        raise InstallError(f"{stage} failed (exit {result.returncode}). See the command output above.",
                           result.returncode)


def list_devices(execute=run) -> list[Device]:
    with tempfile.TemporaryDirectory(prefix="jobswipe-devices-") as directory:
        output = Path(directory) / "devices.json"
        checked("Read devices", ["xcrun", "devicectl", "list", "devices", "--timeout", "30",
                                  "--json-output", str(output), "--omit-deprecated-fields-in-json"],
                execute, timeout=40)
        try:
            document = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise InstallError(f"Cannot read devicectl device JSON: {error}") from error
    return parse_devices(document)


def _refresh_connection(device: Device, execute) -> Device:
    with tempfile.TemporaryDirectory(prefix="jobswipe-device-details-") as directory:
        output = Path(directory) / "details.json"
        checked("Refresh selected iPhone connection", [
            "xcrun", "devicectl", "device", "info", "details", "--device", device.identifier,
            "--timeout", "20", "--json-output", str(output), "--omit-deprecated-fields-in-json",
        ], execute, timeout=30)
        try:
            document = _object(json.loads(output.read_text(encoding="utf-8")), "document")
        except (OSError, ValueError) as error:
            raise InstallError(f"Cannot read devicectl device details JSON: {error}") from error
    info = _object(document.get("info"), "info")
    if info.get("outcome") != "success":
        raise InstallError("devicectl did not report successful device details.")
    refreshed = parse_device(document.get("result"))
    if refreshed.identifier != device.identifier or refreshed.udid != device.udid:
        raise InstallError("Refreshed device identifier or UDID does not match the selected iPhone; installation stopped.")
    return refreshed


def select_device(devices: list[Device], requested: str, execute=None) -> Device:
    matches = [device for device in devices if requested in (device.identifier, device.udid)]
    if not matches:
        raise InstallError(f"No device has the exact identifier or UDID {requested!r}. Run without arguments to list devices.")
    if len(matches) != 1:
        raise InstallError(f"Device identifier {requested!r} is ambiguous; installation stopped.")
    device = matches[0]
    # Only the exact, paired physical iPhone is eligible for a connection refresh.
    blockers = device.blockers(require_connection=False)
    if blockers:
        raise InstallError("Device cannot be used: " + "; ".join(blockers) + ".")
    if device.connection != "connected" and execute is not None:
        device = _refresh_connection(device, execute)
    blockers = device.blockers()
    if blockers:
        raise InstallError("Device cannot be used: " + "; ".join(blockers) + ".")
    return device


def install(device: Device, execute=run, root: Path = ROOT) -> None:
    # Recheck at this boundary so callers cannot bypass the physical-device gate.
    if device.blockers():
        raise InstallError("Device cannot be used: " + "; ".join(device.blockers()) + ".")
    derived = root / ".build/device"
    app = derived / "Build/Products/Debug-iphoneos/jobswipe.app"
    checked("Build signed iPhone app", [
        "xcodebuild", "-project", str(root / "JobReview.xcodeproj"),
        "-scheme", "JobReview", "-configuration", "Debug",
        "-destination", f"platform=iOS,id={device.udid}",
        "-derivedDataPath", str(derived), "-allowProvisioningUpdates",
        "-allowProvisioningDeviceRegistration", "build",
    ], execute, timeout=1800)
    checked("Verify app signature", ["codesign", "--verify", "--deep", "--strict", str(app)], execute)
    try:
        with (app / "Info.plist").open("rb") as source:
            info = plistlib.load(source)
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
        raise InstallError(f"Cannot read built app Info.plist: {error}") from error
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise InstallError(f"Built app bundle ID does not equal {BUNDLE_ID}; installation stopped.")
    checked("Install jobswipe", ["xcrun", "devicectl", "device", "install", "app",
                                 "--device", device.identifier, str(app)], execute)
    print("jobswipe installed. Launching it next.", flush=True)
    checked("Launch jobswipe", ["xcrun", "devicectl", "device", "process", "launch",
                                "--device", device.identifier, "--terminate-existing", BUNDLE_ID], execute)
    print(f"jobswipe installed and launched on {device.name or device.identifier}.")


def main(argv=None, execute=run, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="Exact CoreDevice identifier or hardware UDID from the list.")
    parser.add_argument("--install", action="store_true",
                        help="Build using existing Xcode signing, then install and launch on the selected iPhone.")
    args = parser.parse_args(argv)
    if args.install and not args.device:
        parser.error("--install requires an explicit --device ID")
    try:
        devices = list_devices(execute)
        if args.device:
            device = select_device(devices, args.device, execute)
            print(f"Selected physical iPhone: {device.name or device.identifier} ({device.udid})")
            if args.install:
                install(device, execute, root)
            else:
                print("Device is connected and paired. No build or installation was run.")
                print("Signing, provisioning, Developer Mode, and launch are checked when --install runs.")
            return 0
        for device in devices:
            print(f"{device.name or 'Unnamed'} | ID: {device.identifier} | UDID: {device.udid or 'unknown'}")
            print("  " + ("; ".join(device.blockers()) or "Physical iPhone connected and paired"))
        if not any(not device.blockers() for device in devices):
            print("No connected, paired physical iPhone is available. Connect and unlock your iPhone, then trust this Mac.")
            return 2
        print("No device was selected. Use --device ID to check one; add --install to build, install, and launch.")
        return 0
    except InstallError as error:
        print(f"Error: {error}", file=sys.stderr)
        return error.returncode


if __name__ == "__main__":
    raise SystemExit(main())
