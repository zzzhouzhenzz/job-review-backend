#!/usr/bin/env python3
"""Run fictional unit checks and inspect the finished app. No production requests."""
import os
from pathlib import Path
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]
env = os.environ.copy()
env['CLANG_MODULE_CACHE_PATH'] = str(ROOT / '.build/module-cache')
env['SWIFTPM_MODULECACHE_OVERRIDE'] = str(ROOT / '.build/module-cache')
subprocess.run(['swift', 'test', '--package-path', str(ROOT), '--disable-sandbox', '--cache-path', str(ROOT / '.build/pm-cache')], env=env, check=True)
app = ROOT / 'dist/Job Review.app'
if app.exists():
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    with (app / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    assert info['CFBundleIdentifier'] == 'app.jobreview.mac'
    files = sorted(p.relative_to(app).as_posix() for p in app.rglob('*') if p.is_file())
    allowed = {'Contents/Info.plist', 'Contents/MacOS/JobReviewMac', 'Contents/Resources/AppIcon.icns', 'Contents/_CodeSignature/CodeResources'}
    assert set(files) == allowed, f'Unexpected bundle files: {set(files) - allowed}'
    print('Bundle file allowlist and strict signature: passed')
else:
    print('App bundle absent; run scripts/build.py for packaging checks.')
