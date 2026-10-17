# Job Review backend

Source-only export for deploying jobswipe on Linux. This repository contains the
Python backend, static legacy UI, API contracts, tests, and fictional fixtures.
It does not contain a database, job research, user decisions, credentials,
browser profiles, macOS service configuration, or platform-specific binaries.

Read [LINUX_SETUP_PROMPT.md](LINUX_SETUP_PROMPT.md) for the deployment task.
`SOURCE_MANIFEST.json` records the exact source files in this export. The existing
`job-review/README.md` includes historical Mac deployment instructions; its older
deployment-status statements are not the current status of either machine.

## Runtime

- Python 3.10 or later is the deployment target. The backend uses the standard
  library; external TLS/browser/desktop tools are separate dependencies.
- SQLite holds jobs, decisions, durable commands, tasks, questions, and receipts.
- `job-review/tailscale_backend.py` supervises the authenticated API on loopback.
- Tailscale Serve supplies private HTTPS. The Caddy implementation is also
  included, but no macOS Caddy binary is shipped.
- Agent, LLM, and Chrome execution adapters remain unimplemented. The default
  worker reports `not_configured`; it does not apply to employers or crawl jobs.

## Source verification

From the repository root:

```sh
python3 -B -m unittest discover -s job-review -p 'test_*.py'
node job-review/test_ui.js
```

Tests use temporary fictional databases. Real Caddy tests require a verified
Linux Caddy executable supplied through `JOB_REVIEW_CADDY`, OpenSSL, and an
HTTP/2-capable curl for HTTP/2 coverage. Report skips separately from passes.

## Deployment entry point

Generate a private device-access configuration with `device_access.py` only after
the Linux HTTPS origin is known. Inspect each command's `--help` before using it.

```sh
python3 job-review/tailscale_backend.py check \
  --db /absolute/private/state/review.sqlite3 \
  --access-config /absolute/private/config/access.json \
  --credential-file /absolute/private/config/credential.txt \
  --port 8766
```

`run` and `status` accept the same arguments. `run` creates a verified backup
before migration and holds a database lock. `status` only checks the local API;
it does not prove remote TLS or phone access.

Move production data separately through an authenticated private channel. Use a
consistent SQLite backup, preserve all tables and request receipts, and perform
a coordinated final cutover. Do not copy a live database file while ignoring its
WAL. Do not run independent writable Mac and Linux copies as one logical server.

## Remote Chrome design

The target design is a dedicated TigerVNC virtual display, a lightweight window
manager, and headed Google Chrome with one private persistent profile. noVNC and
websockify make that same session viewable from a Mac browser. A separate private
Tailscale HTTPS listener protects the desktop route. This design still requires
Linux implementation and verification; it is not part of the current Python
runtime.

References:

- [TigerVNC virtual X server](https://tigervnc.org/doc/Xvnc.html)
- [noVNC deployment and localhost binding](https://github.com/novnc/noVNC)
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
- [Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve)
