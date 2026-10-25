# Job Review

Source for the Job Review backend, native Mac dashboard, and iOS app. The Mac and
iOS clients share the `ReviewCore` package and one backend source of truth.

This repository contains code, tests, fictional fixtures, and build resources.
Production databases, answers, applicant documents, credentials, browser profiles,
native receipts, local signing settings, and deployment history stay outside Git.

## Application workflow

Three application workers retain separate tasks, native sessions, and tabs. All
browser operations enter one durable FIFO queue for a shared Chrome connection.
A worker waiting for an answer releases Chrome after its browser turn is safely
complete. When all required answers are present, it joins the queue tail.
Uncertain browser work stays held until its prior owner is proved stopped.

The applying worker matches questions against the shared question pool. A shared
writer lock covers both worker registration and user answers. Pending questions
and answered questions have Markdown projections. An immutable answered snapshot
and BLAKE3 manifest let a worker detect new answers after acquiring the lock.
Answer storage and wakeup checks run in code; a small model step formats new text.

`GET /api/v1/question-sync` returns a coherent canonical snapshot for both clients.
Revision guards reject stale answers. Identical retries are idempotent. Clients
retain drafts and the last complete snapshot while synchronization is incomplete.
The Mac **Questions** entry point supports pending questions and saved answers;
**Workflow** displays the three workers, Chrome owner/queue, and shared state.

Browser preparation reads existing evidence files on demand. A resumed worker
verifies its exact current tab in the preparation turn before any form action.
The separate initial tab binding and final submission guards remain in place.

## Layout

- `job-review/`: Python API, workflow storage, worker runtime, contracts, and tests.
- `job-review-ios/`: shared `ReviewCore`, SwiftUI iOS app, simulator project, and tests.
- `job-review-mac/`: SwiftUI/AppKit dashboard, Questions editor, live workflow, and tests.
- `SOURCE_MANIFEST.json`: SHA-256 hashes of the exported source files.
- `VERIFICATION.md`: checks performed on this export and their limits.

## Build and test

Use Python 3.10 or later. The shared answer store requires the pinned BLAKE3 library:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r job-review/requirements-pool.txt
.venv/bin/python -B -m unittest discover -s job-review -p 'test_*.py'
node job-review/test_ui.js
```

Tests use fictional temporary databases. HTTP tests need loopback sockets. Optional
Caddy tests require `JOB_REVIEW_CADDY`, OpenSSL, and an HTTP/2-capable curl where
applicable. Report skipped checks separately from passes.

On macOS 14 or later with Xcode:

```sh
python3 job-review-mac/scripts/verify.py
swift test --package-path job-review-ios --disable-sandbox
python3 job-review-mac/scripts/build.py
python3 job-review-ios/scripts/check_ios.py --build
```

The Mac package uses the sibling iOS package. An unsigned simulator build does
not require a signing team. Select your own team locally before installing on a
physical iPhone. Font licenses and asset provenance are included with the assets.

## Deployment

See `job-review/README.md` for private API setup and runtime configuration. The
runtime example contains placeholders; fill them in a private configuration file.
A runtime config alone does not grant browser-worker capability. Native proof
must match the target host, binaries, browser, configuration, and source. Exact
reviewed runtime compatibility is documented in `job-review/PROOF_COMPATIBILITY.md`.

The current native Chrome integration is verified in a supported Mac desktop
environment. A source export does not establish Linux browser support. Keep Linux
workers disabled until that environment has independent evidence. The historical
`LINUX_SETUP_PROMPT.md` describes a separate deployment task, not an executed step.

Move production data through a private authenticated channel. Use SQLite's backup
API or a coordinated stop; preserve WAL data, request receipts, answer scopes, and
submission history. Submitted, uncertain, or quarantined applications must never
be reset merely to run a smoke test. Do not run two writable copies as one server.
