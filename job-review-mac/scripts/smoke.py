#!/usr/bin/env python3
"""Run the app's fictional window checks and render its own SwiftUI view.

Does not capture the screen, inspect other applications, or contact the backend.
The optional service PID check observes only launchd status.
"""
import json
from pathlib import Path
import re
import subprocess
import os
import argparse

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--binary', type=Path, default=ROOT / 'dist/Job Review.app/Contents/MacOS/JobReviewMac')
parser.add_argument('--output-dir', type=Path, default=ROOT / '.build/ui-verification')
parser.add_argument('--require-server-stopped', action='store_true', help='Assert production stays unloaded with no port 8766 listener')
parser.add_argument('--only', action='append', default=[], help='Run only this named scenario; repeat to select more than one')
options = parser.parse_args()
output = options.output_dir
output.mkdir(parents=True, exist_ok=True)
binary = options.binary.resolve()

def backend_pid():
    result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/app.jobreview.backend'], capture_output=True, text=True)
    match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None

before = backend_pid()
def assert_server_stopped():
    agent = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/app.jobreview.backend'], capture_output=True, text=True)
    port = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:8766', '-sTCP:LISTEN', '-Fp'], capture_output=True, text=True)
    assert agent.returncode == 113 and 'Could not find service "app.jobreview.backend"' in agent.stdout + agent.stderr
    assert port.returncode == 1 and not (port.stdout + port.stderr).strip()
if options.require_server_stopped:
    assert_server_stopped()
reports = []
for name, flags in [
    *[(name, ['--page=workflow', f'--scenario={name}', *flags]) for name, flags in [
        ('workflow', []), ('workflow-full', ['--tall']), ('workflow-publishing', ['--appearance=dark']),
        ('workflow-idle', ['--compact']), ('workflow-stale', ['--compact']),
        ('workflow-unsupported', []), ('workflow-disconnected', []), ('workflow-unknown', ['--compact'])]],
    ('workflow-browser-queue', ['--page=workflow', '--scenario=workflow-browser-queue', '--tall']),
    ('workflow-browser-compact', ['--page=workflow', '--scenario=workflow-browser-queue', '--compact']),
    ('workflow-browser-blocked', ['--page=workflow', '--scenario=workflow-browser-blocked', '--appearance=dark']),
    ('workflow-browser-unknown', ['--page=workflow', '--scenario=workflow-browser-unknown', '--compact']),
    ('board', []), ('board-compact', ['--compact']),
    ('board-details', ['--page=board', '--inspect-task', '--compact']),
    ('overview', ['--page=overview']),
    ('overview-dark', ['--page=overview', '--appearance=dark']),
    ('board-dark', ['--appearance=dark']),
    ('board-collapsed', ['--collapsed-sidebar']),
    ('board-compact-dark', ['--compact', '--appearance=dark', '--collapsed-sidebar']),
    ('browse-dark', ['--page=browse', '--appearance=dark', '--compact']),
    ('browse-search', ['--page=browse', '--verify-browse-search']),
    ('connection-idle', ['--page=connection', '--scenario=idle']),
    ('connection-paused', ['--page=connection', '--scenario=workers-paused']),
    ('connection-not-configured', ['--page=connection', '--scenario=workers-not-configured']),
    ('stale', ['--scenario=stale']), ('disconnected', ['--scenario=disconnected']),
    ('loading', ['--scenario=loading']), ('feed', ['--page=feed']), ('activity', ['--page=activity']),
    ('browse-compact', ['--page=browse', '--compact']),
    ('browse-multiple', ['--page=browse', '--scenario=browse-multiple']),
    ('browse-stale', ['--page=browse', '--scenario=stale']),
    ('questions', ['--page=board', '--inspect-task', '--scenario=questions', '--compact']),
    ('questions-sync', ['--page=board', '--scenario=questions', '--verify-question-sync', '--compact']),
    ('questions-saved', ['--page=board', '--inspect-task', '--scenario=questions-saved', '--compact']),
    ('questions-saved-dark', ['--page=board', '--inspect-task', '--scenario=questions-saved', '--appearance=dark']),
    ('questions-accepted-blocked', ['--page=board', '--inspect-task', '--scenario=questions-accepted-blocked', '--compact']),
    ('questions-inbox', ['--page=questions', '--scenario=questions-inbox']),
    *[(f'canonical-questions-{state}', ['--page=questions', f'--scenario=canonical-questions-{state}', '--compact']) for state in ['ready', 'saved', 'loading', 'updating', 'pending', 'failed', 'retained']],
    ('questions-inbox-compact', ['--page=questions', '--scenario=questions-inbox', '--compact', '--inspect-question']),
    ('questions-inbox-saved', ['--page=questions', '--scenario=questions-inbox', '--questions-filter=saved', '--appearance=dark', '--inspect-question']),
    ('questions-inbox-issues', ['--page=questions', '--scenario=questions-inbox', '--questions-filter=issues', '--compact', '--inspect-question']),
    ('questions-inbox-navigation', ['--page=questions', '--scenario=questions-inbox', '--verify-questions-inbox']),
    ('questions-inbox-empty', ['--page=questions', '--scenario=idle', '--compact']),
    ('questions-inbox-mixed', ['--page=questions', '--scenario=questions-inbox-mixed', '--questions-filter=saved', '--compact', '--inspect-question']),
    ('questions-inbox-sync-wait', ['--page=questions', '--scenario=questions-inbox-mixed', '--compact', '--inspect-question']),
    ('instructions', ['--verify-instructions', '--compact']),
    ('instructions-saved', ['--verify-instructions', '--submit-demo-instructions']),
    ('instructions-preparation', ['--verify-instructions', '--submit-demo-instructions', '--scenario=instructions-preparation']),
    ('instructions-uncertain-dark', ['--verify-instructions', '--scenario=instructions-uncertain', '--appearance=dark']),
    ('instructions-error', ['--verify-instructions', '--scenario=instructions-error']),
    ('server-stopped', ['--scenario=server-stopped', '--compact']),
    ('server-controls', ['--scenario=server-stopped', '--verify-server-controls']),
    ('server-start-error', ['--scenario=server-start-error', '--verify-server-controls']),
    ('server-running-dark', ['--appearance=dark', '--compact']),
    ('consent-first-use', ['--scenario=consent-first-use', '--page=connection']),
    ('consent-agreed', ['--scenario=consent-agreed', '--page=connection']),
    ('consent-declined', ['--scenario=consent-declined', '--page=connection']),
    ('consent-save-failure', ['--scenario=consent-save-failure', '--page=connection'])
]:
    if options.only and name not in options.only:
        continue
    report_path = output / f'{name}.json'
    image_path = output / f'{name}.png'
    # Each test process exits via NSApplication.terminate, exercising frontend quit.
    result = subprocess.run([str(binary), '--demo', '--ui-smoke', '--exit-after-smoke',
                             f'--smoke-report={report_path}', f'--render={image_path}', *flags],
                            capture_output=True, text=True, timeout=25)
    if result.returncode != 0:
        raise RuntimeError(f'{name}: native app exited with status {result.returncode}: {result.stderr[:500]}')
    report = json.loads(report_path.read_text())
    report['scenario'] = name
    report['view_rendered'] = image_path.exists() and image_path.stat().st_size > 1000
    # A locked Mac can prevent focus. Keep this result visible rather than fabricating success.
    required = {k: v for k, v in report.items() if k not in {'dashboard_has_focus', 'scenario'}}
    assert all(v is True for v in required.values()), report
    reports.append(report)
    if options.require_server_stopped:
        assert_server_stopped()
    print(f'{name}: window/lifecycle/render passed; key window={report["dashboard_has_focus"]}')
after = backend_pid()
missing = set(options.only) - {report['scenario'] for report in reports}
if missing:
    raise ValueError('Unknown smoke scenarios: ' + ', '.join(sorted(missing)))
summary = {'scenarios': reports, 'backend_pid_before': before, 'backend_pid_after': after,
           'backend_same_process': before is not None and before == after,
           'backend_remained_stopped': options.require_server_stopped}
(output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(f'Backend remained the same process: {summary["backend_same_process"]}')
