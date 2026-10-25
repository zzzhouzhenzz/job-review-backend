#!/usr/bin/env python3
"""Exercise a harmless, temporary LaunchAgent. Never start or stop production.

The dummy writes a SIGTERM marker and exits. Production must be unloaded and
port 8766 free before and after this check. No API or database is accessed.
"""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
TARGET = f'gui/{os.getuid()}/app.jobreview.backend'

def command(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=15)

def production_stopped():
    agent = command('/bin/launchctl', 'print', TARGET)
    port = command('/usr/sbin/lsof', '-nP', '-iTCP:8766', '-sTCP:LISTEN', '-Fp')
    assert agent.returncode == 113 and 'Could not find service "app.jobreview.backend"' in agent.stderr + agent.stdout
    assert port.returncode == 1 and not (port.stdout + port.stderr).strip()
    return True

def wait_for(check):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError('Dummy lifecycle check timed out')

before = production_stopped()
label = 'app.jobreview.mac-test-' + uuid.uuid4().hex
target = f'gui/{os.getuid()}/{label}'
with tempfile.TemporaryDirectory(prefix='job-review-mac-lifecycle-') as temporary:
    folder = Path(temporary)
    script, marker, ready = folder / 'dummy.py', folder / 'sigterm', folder / 'ready'
    script.write_text('import signal, time\nfrom pathlib import Path\n'
                      f'def stop(*args):\n    Path({str(marker)!r}).write_text("SIGTERM")\n    raise SystemExit(0)\n'
                      'signal.signal(signal.SIGTERM, stop)\n'
                      f'Path({str(ready)!r}).write_text("ready")\n'
                      'while True: time.sleep(0.1)\n')
    plist = folder / f'{label}.plist'
    args = ['/usr/bin/python3', '-B', str(script)]
    plist.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': args,
        'RunAtLoad': True, 'KeepAlive': True, 'ExitTimeOut': 5,
        'StandardOutPath': str(folder / 'out'), 'StandardErrorPath': str(folder / 'err')}))
    try:
        loaded = command('/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist))
        assert loaded.returncode == 0, loaded.stderr
        wait_for(ready.exists)
        registered = command('/bin/launchctl', 'print', target)
        assert registered.returncode == 0
        lines = [line.strip() for line in registered.stdout.splitlines()]
        first = lines.index('arguments = {')
        last = lines.index('}', first)
        assert lines[first + 1:last] == args, 'Unexpected launchctl argument format'
        assert 'path = ' + str(plist.resolve()) in lines
        stopped = command('/bin/launchctl', 'bootout', target)
        assert stopped.returncode == 0, stopped.stderr
        wait_for(marker.exists)
        assert marker.read_text() == 'SIGTERM'
        assert command('/bin/launchctl', 'print', target).returncode == 113
        time.sleep(1)
        assert command('/bin/launchctl', 'print', target).returncode == 113, 'KeepAlive reloaded the dummy'
    finally:
        # Cleanup can address only the unique dummy service, never TARGET.
        if command('/bin/launchctl', 'print', target).returncode == 0:
            command('/bin/launchctl', 'bootout', target)
after = production_stopped()
report = {'dummy_bootstrap': True, 'launchctl_format_verified': True, 'dummy_sigterm_received': True,
          'dummy_unloaded_without_keepalive_restart': True, 'production_stopped_before': before,
          'production_stopped_after': after, 'production_lifecycle_commands': 0}
(ROOT / '.build/server-controls-dummy-lifecycle.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
