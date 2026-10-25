#!/usr/bin/env python3
"""Build a local, ad-hoc signed native app. Never reads deployment credentials."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build'
BUILD.mkdir(exist_ok=True)
env = os.environ.copy()
env['CLANG_MODULE_CACHE_PATH'] = str(BUILD / 'module-cache')
env['SWIFTPM_MODULECACHE_OVERRIDE'] = str(BUILD / 'module-cache')
command = ['swift', 'build', '--package-path', str(ROOT), '--disable-sandbox', '--cache-path', str(BUILD / 'pm-cache'), '-c', 'release']
subprocess.run(command, env=env, check=True)
result = subprocess.run(command + ['--show-bin-path'], env=env, capture_output=True, text=True, check=True)
bin_path = Path(result.stdout.strip())
app = ROOT / 'dist' / 'Job Review.app'
contents = app / 'Contents'
macos = contents / 'MacOS'
resources = contents / 'Resources'
macos.mkdir(parents=True, exist_ok=True)
resources.mkdir(parents=True, exist_ok=True)
shutil.copy2(bin_path / 'JobReviewMac', macos / 'JobReviewMac')
with (contents / 'Info.plist').open('wb') as stream:
    plistlib.dump({'CFBundleIdentifier': 'app.jobreview.mac', 'CFBundleName': 'Job Review',
                  'CFBundleDisplayName': 'Job Review', 'CFBundleExecutable': 'JobReviewMac',
                  'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '1.9.0',
                  'CFBundleVersion': '14', 'LSMinimumSystemVersion': '14.0',
                  'NSHighResolutionCapable': True, 'NSPrincipalClass': 'NSApplication',
                  'CFBundleIconFile': 'AppIcon',
                  'NSHumanReadableCopyright': 'Job Review'}, stream)
# The linked ReviewCore models do not load its demo resources. Bundle no data/config files.
subprocess.run(['swift', str(ROOT / 'scripts' / 'make_icon.swift'), str(BUILD / 'AppIcon.iconset')], env=env, check=True)
subprocess.run(['iconutil', '-c', 'icns', str(BUILD / 'AppIcon.iconset'), '-o', str(resources / 'AppIcon.icns')], check=True)
subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(app)], check=True)
subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
print(app)
