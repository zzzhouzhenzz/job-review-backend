Set up this Linux machine as the backend host for my jobswipe / Job Review
system. Execute the setup and verification; do not stop at a design or a list of
commands. Keep changes confined to this deployment and preserve other services.

## Source and current system

Clone the private repository `zzzhouzhenzz/job-review-backend` from GitHub into
`/home/zz/ml-workspace/job-review-backend`, if that directory does not already
exist. If it exists, inspect its remote and changes before reusing it. Never
reset unrelated work. Use the immutable source commit supplied with this prompt
if present; otherwise record the fetched commit. Verify `SOURCE_MANIFEST.json`.
Create a `codex/linux-deployment` branch for deployment changes.

Read applicable AGENTS.md files, the root README, `job-review/CONTRACT_V1.md`,
`job-review/CONTRACT.md`, and the actual implementation. Treat old Mac deployment
notes in the legacy README as historical. Do not recreate the backend.

The backend already implements SQLite persistence, authenticated APIs,
idempotent commands, application tracking, typed questions/answers, ranking
contracts, health checks, and durable task scheduling. The iOS app already uses
this API. Preserve its contracts and retry semantics. Agent, LLM, and Chrome
execution adapters are deliberately unimplemented and must remain disabled for
this deployment. A queued Apply is not an employer submission.

GitHub is for code, tests, templates, and nonsecret documentation only. Production
databases, decisions, research, browser profiles, cookies, tokens, private keys,
resumes, and account credentials must stay out of Git and artifacts.

## Target architecture

1. iPhone on Tailscale -> private Linux HTTPS endpoint -> authenticated Python
   API bound to `127.0.0.1:8766` -> private SQLite database.
2. Mac browser on Tailscale -> separate private HTTPS desktop endpoint -> noVNC
   and websockify -> a localhost-only VNC server -> dedicated Linux virtual
   desktop -> headed Google Chrome with a persistent LinkedIn profile.
3. A future Linux agent must attach to that same Chrome process/profile. The
   remote desktop is a view of the actual browser session, not a second browser.

Use Tailscale Serve for both HTTPS routes. Discover this Linux machine's actual
tailnet DNS name; do not reuse the Mac's hostname. Prefer HTTPS port 443 for the
API and 8443 for the desktop if free. Inspect existing Serve configuration and
ports first. Preserve existing routes; never run a blanket reset. Do not enable
Funnel, public router forwarding, or a public remote desktop.

## Inspect and prepare

Inspect the Linux distribution, architecture, Python, systemd, user/session
environment, Tailscale state, Chrome installation, existing displays, ports,
services, and available Codex browser tools. Use actual local help and official
documentation rather than guessing package names or flags.

Reuse suitable installed tools. Install necessary packages from official vendor
or distribution sources. Use Python 3.10+ and an isolated environment if needed.
Do not assume macOS binaries or launchd configuration work on Linux.

Implement small, repeatable setup/status/start/stop helpers and systemd units.
Run the API and browser as an unprivileged user. Make the API start without an
interactive terminal, restart after failure, and stop cleanly. Choose systemd
user services with linger or system services with an explicit unprivileged User
based on the host. Explain any required administrator action before it is needed.
Keep API availability independent of whether Chrome or an agent is ready.

## Backend and private access

Use `job-review/tailscale_backend.py` as the API supervisor. It validates private
files, takes a database lock, backs up the database, and binds only to loopback.
Do not replace it with an unauthenticated `start.py` service.

Use private configuration/state directories outside the Git checkout, with
directory mode 0700 and secret-file mode 0600. After discovering the real Linux
HTTPS origin, create the access-config/credential pair using `device_access.py`.
Inspect its `--help`. Pass credential FILE PATHS to commands, not token values.
Retain bearer authentication, exact Origin checks, session-token validation,
certificate validation, request limits, and exact-byte idempotent replay.

Do not print secrets in chat, logs, argv, service units, URLs, screenshots, or
Git. Provision the iPhone credential locally into Keychain. The Linux origin is
different from the Mac origin, so the iOS app treats it as a separate connection.
Do not clear pending phone requests or change their origin to force cutover.

## Persistent virtual desktop and Chrome

Use TigerVNC's virtual X server and a lightweight window manager such as Openbox,
with noVNC/websockify for access from a normal Mac browser. If the installed
distribution provides a better-supported equivalent, explain the choice before
using it. Choose a free dedicated display rather than exposing my normal Linux
desktop. Bind X/VNC/websockify listeners to localhost or private Unix sockets;
disable unnecessary X TCP listening. Put the web viewer behind private HTTPS.
Require a separate desktop authentication secret through the VNC stack and
restrict access to my intended tailnet devices/user. Do not weaken existing ACLs.

Use a dedicated profile, for example
`/home/zz/.local/share/job-review/chrome-profile`, owned by the service user with
mode 0700. Launch normal headed Google Chrome in this virtual desktop as a
non-root user. Keep Chrome's sandbox enabled. Do not use `--no-sandbox`, publish
an unauthenticated remote-debugging port, or copy the Mac's Chrome profile,
cookies, passwords, or tokens. Configure the supported per-user DBus/credential
store where needed; do not weaken browser credential storage to bypass prompts.

Give me the private desktop URL when it works. I will sign in to LinkedIn inside
this Linux Chrome session and handle passwords, MFA, CAPTCHAs, and account
prompts myself. Warn me before these human steps and pause browser automation
while I handle them. Preserve the profile across restarts. Disconnecting the Mac
viewer must not close Chrome or its virtual desktop.

Inspect the actual Codex Chrome connector/plugin available on Linux. Verify
whether it can attach to this exact profile/session. Do not assume Mac extension
support proves Linux support, and do not invent a tool/API or use a second login
session. If supported, verify a read-only page read from the same window visible
in noVNC. If unsupported, report the exact limitation while completing the API
and remote desktop. Do not claim the backend Chrome health check is ready while
its adapter is unimplemented. Maintain one browser-controller owner at a time,
with an explicit pause/takeover process for human interaction.

## Production data and cutover

This GitHub repository intentionally contains no production database. Initially
use a separate staging database with fictional data or an empty staging database.
Do not present it as the migrated production feed.

Inspect whether an existing Linux production database is present before writing
anything. To preserve the current Mac history, obtain a consistent SQLite backup
from the Mac over an authenticated private transfer, not GitHub. If Mac access or
the transfer route is missing, finish the independent Linux setup first and ask
only for that missing input. Do not enable SSH or access the Mac without an
authorized route. Do not create a fake replacement catalog.

Before final cutover, coordinate a brief pause in iPhone writes and resolve any
pending requests against the old server. Take the final SQLite backup using the
backup API, or stop the verified old service before a consistent copy. Verify
checksums, integrity, table row counts, decisions, command receipts, questions,
processes, and task IDs. Never copy just a live SQLite main file and ignore WAL.
Use one authoritative writable server. A rehearsal copy is not the final copy.

Start Linux with the verified final database and keep all worker adapters
disabled. Provision the new Linux HTTPS origin and credential in the iPhone
only after pending saves are resolved and the final snapshot is ready. Verify
real feed/status reads without test decisions. Retain the Mac backup and a
documented rollback plan. Do not stop or change the Mac service without separate
coordination. If rollback follows Linux writes, preserve those new writes before
switching back; never silently discard or merge divergent database copies.

## Verification and completion

Run the existing Python tests from the repository root and the legacy UI tests
if Node is available. Use temporary fictional data for writes. Include real TLS
and HTTP/2 checks with supported Linux tools; report missing coverage/skips.
Do not run test Apply commands on production or submit employer applications.

Verify and record:
- API 401 without credentials and 200 with valid credentials through the actual
  HTTPS origin, with normal certificate and hostname validation.
- Exact-job Apply, same-ID replay, answer types, and cancellation using only
  fictional test data; no duplicate process after a lost response or retry.
- The live API remains available while Chrome/agents are unavailable, and health
  reports `not_configured` honestly until adapters exist.
- Services restart cleanly and persist without an SSH terminal. Inspect boot
  configuration; do not reboot the whole machine without my confirmation.
- The desktop works from my Mac, accepts mouse/keyboard input for a harmless
  check, disconnects/reconnects to the same running Chrome session, and retains
  the profile across a service restart. Ask me for a remote observation only
  when your tools cannot verify it. Do not claim a local check proves Mac access.
- After my login, the same Chrome session can read a LinkedIn job posting. Keep
  actual browser observation separate from unimplemented backend readiness.
- No raw API, VNC, X, or browser-debugging listener is exposed publicly.
- Migrated production history matches the final snapshot, and the iPhone can
  read it over Wi-Fi and cellular when available for testing.

Write a concise runbook with exact service names, start/stop/restart/status/log
commands, private config paths, backup/restore procedure, URLs, profile path,
credential rotation, browser takeover, tested results, and remaining limitations.
Commit and push only deployment code, tests, templates, and nonsecret runbook
changes to `codex/linux-deployment`; leave main and private state untouched.

Proceed autonomously with reversible setup. Ask only for missing access, secrets,
human login/approval steps, or a decision that changes scope. If one step blocks,
continue independent work and report the exact blocker. Do not describe the full
setup as complete until the relevant end-to-end checks have passed.
