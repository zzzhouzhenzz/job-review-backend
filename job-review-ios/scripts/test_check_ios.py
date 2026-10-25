"""Checks for the build gate; no SDK, device, or real build is required."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import check_ios


def tools(*, xcode=True, sdk=True, xcode_major=16, sdk_major=18):
    def execute(command, environment):
        if command[0] == "xcode-select":
            return subprocess.CompletedProcess(command, 0, "/Applications/Xcode.app/Contents/Developer\n", "")
        if command[0] == "xcodebuild":
            return subprocess.CompletedProcess(command, 0 if xcode else 1, f"Xcode {xcode_major}.4\nBuild version test\n" if xcode else "", "" if xcode else "Full Xcode is required")
        if command[-1] == "--show-sdk-version":
            return subprocess.CompletedProcess(command, 0 if sdk else 1, f"{sdk_major}.0\n" if sdk else "", "" if sdk else "SDK cannot be located")
        return subprocess.CompletedProcess(command, 0 if sdk else 1, "/SDKs/iPhoneSimulator.sdk\n" if sdk else "", "" if sdk else "SDK cannot be located")
    return execute


class BuildGateChecks(unittest.TestCase):
    def test_command_line_tools_cannot_pass(self):
        report = check_ios.inspect({}, tools(xcode=False, sdk=False))
        self.assertFalse(report["ready_for_simulator_build"])
        self.assertEqual(len(report["blockers"]), 2)

    def test_xcode_without_sdk_cannot_pass(self):
        report = check_ios.inspect({}, tools(sdk=False))
        self.assertFalse(report["ready_for_simulator_build"])
        self.assertEqual(len(report["blockers"]), 1)

    def test_check_only_never_builds(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = check_ios.main([], execute=tools(), build=lambda *a, **kw: self.fail("Unexpected build"))
        self.assertEqual(result, 0)

    def test_unsupported_versions_never_build(self):
        for execute in (tools(xcode_major=15), tools(sdk_major=16), tools(sdk_major="unknown")):
            with self.subTest(execute=execute), contextlib.redirect_stdout(io.StringIO()):
                result = check_ios.main(["--build"], execute=execute, build=lambda *a, **kw: self.fail("Unexpected build"))
            self.assertEqual(result, 2)

    def test_blocked_request_never_builds(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = check_ios.main(["--build"], execute=tools(sdk=False), build=lambda *a, **kw: self.fail("Unexpected build"))
        self.assertEqual(result, 2)

    def test_build_failure_and_environment_are_preserved(self):
        captured = []
        def build(command, env):
            captured.append((command, env))
            return 65
        with patch.dict(os.environ, {"DEVELOPER_DIR": "/original"}), contextlib.redirect_stdout(io.StringIO()):
            result = check_ios.main(["--build", "--developer-dir", "/Applications/Xcode.app/Contents/Developer"], execute=tools(), build=build)
            self.assertEqual(os.environ["DEVELOPER_DIR"], "/original")
        self.assertEqual(result, 65)
        self.assertEqual(captured[0][1]["DEVELOPER_DIR"], str(Path("/Applications/Xcode.app/Contents/Developer").resolve()))
        self.assertIn("CODE_SIGNING_ALLOWED=NO", captured[0][0])
        self.assertIn("generic/platform=iOS Simulator", captured[0][0])


if __name__ == "__main__":
    unittest.main()
