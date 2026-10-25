#!/usr/bin/env python3
"""Install the verified frontend for the current user and reopen it.

Only processes executing the exact installed frontend path are stopped.
The backend service is not managed by this script. Keep the previous app for rollback.
"""
import datetime
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid
import argparse

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--defer-relaunch', action='store_true', help='Install the update while keeping the current frontend and unsent drafts open')
options = parser.parse_args()
source = ROOT / 'dist' / 'Job Review.app'
applications = Path.home() / 'Applications'
destination = applications / 'Job Review.app'
executable = destination / 'Contents/MacOS/JobReviewMac'
subprocess.run(['codesign', '--verify', '--deep', '--strict', str(source)], check=True)
applications.mkdir(exist_ok=True)
if destination.is_symlink():
    raise SystemExit('The installed app is a symlink. No files were changed.')
stage = applications / f'.Job Review.install-{uuid.uuid4().hex}.app'
shutil.copytree(source, stage, symlinks=True)
subprocess.run(['codesign', '--verify', '--deep', '--strict', str(stage)], check=True)
processes = subprocess.run(['ps', '-axo', 'pid=,comm='], check=True, capture_output=True, text=True)
pids = []
for line in processes.stdout.splitlines():
    fields = line.strip().split(maxsplit=1)
    if len(fields) == 2 and fields[1] == str(executable):
        pids.append(int(fields[0]))
for pid in ([] if options.defer_relaunch else pids):
    current = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True, text=True)
    if current.stdout.strip() == str(executable):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
for _ in range(0 if options.defer_relaunch else 40):
    current = subprocess.run(['ps', '-axo', 'comm='], capture_output=True, text=True, check=True)
    if str(executable) not in current.stdout.splitlines():
        break
    time.sleep(0.1)
else:
    if not options.defer_relaunch:
        raise SystemExit(f'The frontend did not exit. Verified update retained at {stage}.')
backup = None
if destination.exists():
    backups = ROOT / '.build/install-backups'
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')
    backup = backups / f'Job Review-{stamp}-{uuid.uuid4().hex[:8]}.app'
    destination.rename(backup)
try:
    stage.rename(destination)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(destination)], check=True)
except Exception:
    if backup is not None and not destination.exists():
        backup.rename(destination)
    raise
if options.defer_relaunch:
    print(f'Installed: {destination}. Current frontend kept running; reopen after preserving unsent drafts.')
else:
    subprocess.run(['open', str(destination)], check=True)
    print(f'Installed and opened: {destination}')
if backup:
    print(f'Previous app kept at: {backup}')
