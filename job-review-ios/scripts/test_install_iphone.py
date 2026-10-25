"""Device selection and stage gates; no real signing, provisioning, or installation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

import install_iphone as installer


def row(identifier="CORE-123", udid="00008101-000A0C123456001E", **hardware):
    return {
        "identifier": identifier,
        "properties": {
            "hardware": {"udid": udid, "deviceType": "iPhone", "platform": "iOS",
                         "reality": "physical", **hardware},
            "connection": {"pairingState": "paired", "state": "connected"},
            "state": {"name": "Test iPhone"},
        },
    }


def document(*rows):
    return {"info": {"outcome": "success"}, "result": {"devices": list(rows)}}


def stale_row(state="disconnected", **hardware):
    device = row(**hardware)
    device["properties"]["connection"]["state"] = state
    return device


def details(device):
    return {"info": {"outcome": "success"}, "result": device}


class Commands:
    def __init__(self, root, *, rows=None, fail=None, bundle_id=installer.BUNDLE_ID,
                 plist_bytes=None, device_json=None, details_json=None, omit_details=False):
        self.root = root
        self.rows = [row()] if rows is None else rows
        self.fail = fail
        self.bundle_id = bundle_id
        self.plist_bytes = plist_bytes
        self.device_json = device_json
        self.details_json = details_json
        self.omit_details = omit_details
        self.calls = []
        self.timeouts = []

    def __call__(self, command, *, timeout):
        self.calls.append(command)
        self.timeouts.append(timeout)
        stage = ("list" if command[1:4] == ["devicectl", "list", "devices"] else
                 "details" if command[1:5] == ["devicectl", "device", "info", "details"] else
                 "build" if command[0] == "xcodebuild" else
                 "sign" if command[0] == "codesign" else
                 "install" if command[3:5] == ["install", "app"] else "launch")
        if stage == self.fail:
            return subprocess.CompletedProcess(command, 65)
        if stage == "list":
            Path(command[command.index("--json-output") + 1]).write_text(
                self.device_json if self.device_json is not None else json.dumps(document(*self.rows)))
        elif stage == "details" and not self.omit_details:
            Path(command[command.index("--json-output") + 1]).write_text(
                self.details_json if self.details_json is not None else json.dumps(details(row())))
        elif stage == "build":
            app = self.root / ".build/device/Build/Products/Debug-iphoneos/jobswipe.app"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(self.plist_bytes if self.plist_bytes is not None
                                             else plistlib.dumps({"CFBundleIdentifier": self.bundle_id}))
        return subprocess.CompletedProcess(command, 0)


class InstallChecks(unittest.TestCase):
    def invoke(self, args, commands):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return installer.main(args, execute=commands, root=commands.root)

    def test_exact_id_or_udid_required(self):
        devices = installer.parse_devices(document(row()))
        self.assertEqual(installer.select_device(devices, "CORE-123"), devices[0])
        self.assertEqual(installer.select_device(devices, devices[0].udid), devices[0])
        for requested in ("CORE", "Test iPhone", "core-123", "missing"):
            with self.subTest(requested=requested), self.assertRaises(installer.InstallError):
                installer.select_device(devices, requested)

    def test_ambiguous_id_or_udid_is_rejected(self):
        for rows in ([row(), row()], [row(), row(identifier="OTHER", udid="CORE-123")]):
            with self.subTest(rows=rows), self.assertRaisesRegex(installer.InstallError, "ambiguous"):
                installer.select_device(installer.parse_devices(document(*rows)), "CORE-123")

    def test_simulated_unknown_non_iphone_or_invalid_udid_is_rejected(self):
        for hardware in ({"reality": "simulated"}, {"reality": None}, {"reality": "future"},
                         {"deviceType": "iPad"}, {"platform": "macOS"},
                         {"udid": None}, {"udid": "PHONE,platform=iOS Simulator"}):
            with self.subTest(hardware=hardware), self.assertRaises(installer.InstallError):
                installer.select_device(installer.parse_devices(document(row(**hardware))), "CORE-123")

    def test_unpaired_or_disconnected_is_rejected(self):
        for key, value in (("pairingState", "unpaired"), ("state", "disconnected"), ("state", None)):
            device = row()
            device["properties"]["connection"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(installer.InstallError):
                installer.select_device(installer.parse_devices(document(device)), "CORE-123")

    def test_invalid_schema_fails_closed(self):
        malformed = [None, [], {}, {"info": {"outcome": "failure"}},
                     {"info": {"outcome": "success"}, "result": {"devices": {}}}]
        for path, value in ((["identifier"], None), (["identifier"], "ID\nInjected"),
                            (["properties"], None), (["properties", "hardware"], []),
                            (["properties", "hardware", "reality"], True),
                            (["properties", "state", "name"], 7)):
            bad_row = copy.deepcopy(row())
            target = bad_row
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            malformed.append(document(bad_row))
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(installer.InstallError):
                installer.parse_devices(value)

    def test_default_and_selected_check_never_build(self):
        with tempfile.TemporaryDirectory() as directory:
            for args in ([], ["--device", "CORE-123"]):
                commands = Commands(Path(directory))
                self.assertEqual(self.invoke(args, commands), 0)
                self.assertEqual(len(commands.calls), 1)

    def test_stale_selected_inventory_refreshes_exact_device_without_build(self):
        with tempfile.TemporaryDirectory() as directory:
            for state in ("disconnected", "available", None):
                for identifier in ("CORE-123", row()["properties"]["hardware"]["udid"]):
                    with self.subTest(state=state, identifier=identifier):
                        commands = Commands(Path(directory), rows=[stale_row(state)])
                        self.assertEqual(self.invoke(["--device", identifier], commands), 0)
                        self.assertEqual(len(commands.calls), 2)
                        refresh = commands.calls[1]
                        self.assertEqual(refresh[:7], ["xcrun", "devicectl", "device", "info", "details",
                                                       "--device", "CORE-123"])
                        self.assertEqual(refresh[refresh.index("--timeout") + 1], "20")
                        self.assertEqual(commands.timeouts[1], 30)

    def test_default_stale_inventory_never_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = Commands(Path(directory), rows=[stale_row()])
            self.assertEqual(self.invoke([], commands), 2)
            self.assertEqual(len(commands.calls), 1)

    def test_untrusted_or_ambiguous_inventory_never_refreshes(self):
        unpaired = stale_row()
        unpaired["properties"]["connection"]["pairingState"] = "unpaired"
        inventories = [[stale_row(reality="simulated")], [stale_row(reality=None)],
                       [stale_row(deviceType="iPad")], [stale_row(platform="macOS")],
                       [stale_row(udid=None)], [unpaired], [stale_row(), stale_row()], []]
        with tempfile.TemporaryDirectory() as directory:
            for rows in inventories:
                with self.subTest(rows=rows):
                    commands = Commands(Path(directory), rows=rows)
                    self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 2)
                    self.assertEqual(len(commands.calls), 1)

    def test_failed_refresh_stops_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = Commands(Path(directory), rows=[stale_row()], fail="details")
            self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 65)
            self.assertEqual(len(commands.calls), 2)

    def test_missing_or_malformed_details_stops_before_build(self):
        options = [{"omit_details": True}]
        options += [{"details_json": content} for content in (
            "not JSON", "null", "{}", json.dumps({"result": row()}),
            json.dumps({"info": {"outcome": "failure"}, "result": row()}),
            json.dumps({"info": {"outcome": "success"}}), json.dumps(details({})),
        )]
        with tempfile.TemporaryDirectory() as directory:
            for option in options:
                with self.subTest(option=option):
                    commands = Commands(Path(directory), rows=[stale_row()], **option)
                    self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 2)
                    self.assertEqual(len(commands.calls), 2)

    def test_refreshed_identity_and_all_device_gates_are_required(self):
        unpaired = row()
        unpaired["properties"]["connection"]["pairingState"] = "unpaired"
        responses = [stale_row(), stale_row("available"), stale_row(None),
                     row(identifier="OTHER"), row(udid="OTHER-UDID"), row(udid=None),
                     row(reality="simulated"), row(reality=None), row(deviceType="iPad"),
                     row(platform="macOS"), unpaired]
        with tempfile.TemporaryDirectory() as directory:
            for response in responses:
                with self.subTest(response=response):
                    commands = Commands(Path(directory), rows=[stale_row()],
                                        details_json=json.dumps(details(response)))
                    self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 2)
                    self.assertEqual(len(commands.calls), 2)

    def test_refresh_timeout_stops_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = Commands(Path(directory), rows=[stale_row()])
            def execute(command, *, timeout):
                if command[3:5] == ["info", "details"]:
                    raise subprocess.TimeoutExpired(command, timeout)
                return commands(command, timeout=timeout)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = installer.main(["--device", "CORE-123", "--install"], execute=execute,
                                        root=commands.root)
            self.assertEqual(result, 2)
            self.assertEqual(len(commands.calls), 1)

    def test_successful_refresh_then_install_uses_the_same_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = Commands(Path(directory), rows=[stale_row()])
            self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 0)
            self.assertEqual(len(commands.calls), 6)
            self.assertIn("platform=iOS,id=00008101-000A0C123456001E", commands.calls[2])
            self.assertEqual(commands.calls[4][5:7], ["--device", "CORE-123"])
            self.assertEqual(commands.calls[5][-4:-2], ["--device", "CORE-123"])

    def test_install_without_device_never_lists_or_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = Commands(Path(directory))
            with self.assertRaises(SystemExit) as error:
                self.invoke(["--install"], commands)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(commands.calls)

    def test_empty_inventory_and_simulators_do_not_allow_install(self):
        with tempfile.TemporaryDirectory() as directory:
            for rows, args in (([], []), ([row(reality="simulated")], []),
                               ([row(reality="simulated")], ["--device", "CORE-123", "--install"])):
                commands = Commands(Path(directory), rows=rows)
                self.assertEqual(self.invoke(args, commands), 2)
                self.assertEqual(len(commands.calls), 1)

    def test_malformed_json_stops_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            for content in ("not JSON", "null", "{}"):
                commands = Commands(Path(directory), device_json=content)
                self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 2)
                self.assertEqual(len(commands.calls), 1)

    def test_each_command_failure_stops_later_stages(self):
        for failure, count in (("list", 1), ("build", 2), ("sign", 3), ("install", 4), ("launch", 5)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                commands = Commands(Path(directory), fail=failure)
                self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 65)
                self.assertEqual(len(commands.calls), count)

    def test_bad_bundle_id_or_plist_stops_before_install(self):
        for options in ({"bundle_id": "another.app"}, {"plist_bytes": b"not a plist"},
                        {"plist_bytes": b'<?xml version="1.0"?><plist><dict><key>bad</dict>'},
                        {"plist_bytes": plistlib.dumps([])}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                commands = Commands(Path(directory), **options)
                self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 2)
                self.assertEqual(len(commands.calls), 3)

    def test_missing_json_or_plist_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def no_output(command, *, timeout):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(installer.InstallError, "device JSON"):
                installer.list_devices(no_output)
            self.assertEqual(len(calls), 1)
            calls.clear()
            device = installer.parse_devices(document(row()))[0]
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(installer.InstallError, "Info.plist"):
                installer.install(device, no_output, Path(directory))
            self.assertEqual(len(calls), 2)

    def test_launch_only_follows_verified_build_and_install(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = Commands(Path(directory))
            self.assertEqual(self.invoke(["--device", "CORE-123", "--install"], commands), 0)
            build, signature, install, launch = commands.calls[1:]
            self.assertIn("platform=iOS,id=00008101-000A0C123456001E", build)
            self.assertIn("-allowProvisioningUpdates", build)
            self.assertIn("-allowProvisioningDeviceRegistration", build)
            self.assertFalse(any("DEVELOPMENT_TEAM" in part or "CODE_SIGN" in part for part in build))
            self.assertEqual(signature[:4], ["codesign", "--verify", "--deep", "--strict"])
            self.assertEqual(install[3:7], ["install", "app", "--device", "CORE-123"])
            self.assertEqual(launch[-4:], ["--device", "CORE-123", "--terminate-existing", installer.BUNDLE_ID])

    def test_spawn_error_and_timeout_are_reported(self):
        for failure in (FileNotFoundError("xcrun missing"), subprocess.TimeoutExpired("xcrun", 40)):
            def execute(command, *, timeout):
                raise failure
            with self.subTest(failure=failure), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(installer.InstallError, "Read devices failed"):
                    installer.list_devices(execute)


if __name__ == "__main__":
    unittest.main()
