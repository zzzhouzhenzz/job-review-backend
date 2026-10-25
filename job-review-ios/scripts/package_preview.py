#!/usr/bin/env python3
"""Package an already-built macOS preview. Do not launch or install it."""
from pathlib import Path
import os
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
environment = os.environ.copy()
environment["CLANG_MODULE_CACHE_PATH"] = str(ROOT / ".build/clang")
environment["SWIFTPM_MODULECACHE_OVERRIDE"] = str(ROOT / ".build/clang")
output = subprocess.check_output(["swift", "build", "--package-path", str(ROOT), "--scratch-path", str(ROOT / ".build/swift"), "--cache-path", str(ROOT / ".build/cache"), "--disable-sandbox", "--show-bin-path"], text=True, env=environment).strip()
binary_directory = Path(output)
app = ROOT / ".build" / "jobswipe Preview.app"
executable = app / "Contents/MacOS/JobReviewPreview"
executable.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(binary_directory / "JobReviewPreview", executable)
# SwiftPM's generated accessor checks the main bundle root before its build directory.
shutil.copytree(binary_directory / "JobReview_ReviewCore.bundle", app / "JobReview_ReviewCore.bundle", dirs_exist_ok=True)
metadata = {
    "CFBundleExecutable": "JobReviewPreview", "CFBundleIdentifier": "app.jobreview.preview",
    "CFBundleName": "jobswipe Preview", "CFBundleDisplayName": "jobswipe Preview",
    "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "14.0", "NSPrincipalClass": "NSApplication", "NSHighResolutionCapable": True,
}
with (app / "Contents/Info.plist").open("wb") as stream:
    plistlib.dump(metadata, stream)
print(app)
