# Job Review for Mac

A native Mac client for the existing Job Review backend. Version 1.9.0 uses the
[CoreUI dashboard](https://21st.dev/@coreui/templates/coreui-free-react-admin-template)
visual style: a dark sidebar, a header with search, breadcrumbs, colored summary
cards, and bordered content panels. It retains job browsing, decisions, answers,
and the Dashboard. It uses SwiftUI for views and AppKit for the window and menu
bar. macOS 14 or later is required.

The Mac and iPhone use the same backend and shared `ReviewCore.WorkflowSession`
for current data, decisions, and recovery. The Mac refreshes every 15 seconds and
when it becomes active. Changes from either device appear after the next
successful refresh. This source export includes both clients and their shared package.

## Open the app

Build from the workspace root:

```sh
python3 job-review-mac/scripts/build.py
open "job-review-mac/dist/Job Review.app"
```

Install or update the copy in `~/Applications`, then reopen it:

```sh
python3 job-review-mac/scripts/install.py
```

The installer keeps the previous app under `.build/install-backups`. It restarts
only the installed Mac frontend. The backend stays independent.

The build creates a local ad-hoc signed app. It is not notarized for distribution.
The executable uses the build Mac's architecture. The build requires Xcode and
its Swift toolchain. The shared `ReviewCore` package is a local dependency in
`../job-review-ios`.

The app opens one dashboard window on the **Dashboard** page. Its task columns are
**Queued**, **Running**, **Needs attention**, and **Finished**. Select a card for
task details. Search matches companies, roles, and task IDs. Columns scroll
vertically; a narrow window also scrolls horizontally. Cards move only when the
backend reports a new status. Quarantined work stays in Needs attention; unknown
states remain visible in an Other column. Finished includes cancelled tasks and
does not mean an application was submitted.

The **Overview** page shows ready jobs, task counts, confirmed submission counts,
recent applications, and recorded activity. All charts and counts use workspace
data. The header search filters companies, roles, and locations; press Return to
open **Browse jobs**. A company outside the search results cannot remain open for
a decision. Use the header appearance menu for Light, Dark, or System mode, and
the sidebar button to collapse navigation.
The app retains one dashboard window. Choose **Open Dashboard** from the **JR** menu
bar item to show or focus it. Closing the window keeps the menu bar and polling
active. Use **Quit Job Review** or **⌘Q** to quit the frontend. The backend remains
running. **⌘R** refreshes; **⌘1** shows the dashboard; **⌘2** opens **Browse**;
**⌘3** opens Workflow; **⌘4** opens Questions; **⌘,** opens Connection.

## Browse and make decisions

Open **Browse** to read company research and job details. Review the jobs, select
those you want, then choose a decision:

- **Apply** authorizes the backend to apply to the selected jobs. It can lead to
  resume uploads and employer application submissions. A saved decision confirms
  the authorization; it does not confirm that an application was submitted.
  Follow application progress and any required questions on the Dashboard.
- **Find more** requests more jobs for the company.
- **Exclude company** excludes the company from further discovery.

Every saved decision consumes the current card and marks all of its jobs as
reviewed, including jobs you did not select. Changes to a card clear the current
selection. Review the current card again before making another decision.

Open **Questions** from the sidebar or menu to see the pending count and a shared
question list. On servers with `question_sync`, **Pending** and **Saved answers**
come directly from the canonical shared snapshot. Saved answers include personal
records without a current application. Each linked form is listed explicitly;
the client does not choose an accepted form or map answer values between forms.
**Task issues** lists blockers that require review outside the answer form.
Search matches question text and linked company names. Submitted, uncertain, and
quarantined applications cannot accept answers. Canonical pending counts describe
shared business state; they do not imply that a particular application can proceed.

**Waiting for sync…** is separate from task status. Updating/pending responses
retain the last complete snapshot and local drafts. Failed or older snapshots
cannot enable answer writes. The request carries the pool revision the user
reviewed; stale rejection refreshes the snapshot without replaying the answer.
Uncertain saves retain their exact request ID, body and original revision.
Older servers retain the existing application-form view and legacy API behavior.

Select a question to open the same editor used by Dashboard. The editor supports
written answers, one choice, and multiple choices. Server-accepted answers are
read-only. When the application still permits answers, choose **Review a correction**
to edit one against its current revision. Reusable suggestions without an accepted
timestamp keep **Use saved answer**. These controls do not submit anything. **Review answers** stays at the bottom of the inspector;
review the exact values, then choose **Submit answers**. Only this final action
sends the answers. You can save valid partial answers; missing required answers
keep the application paused. Empty required draft fields do not block other
valid selections. Task metadata is available under **Task details**.
Only entered answers are sent; untouched optional questions are omitted.
When every answer in the current form is accepted but another task issue remains,
the footer confirms acceptance and names that issue. Open **Task details** for the
next step. Refreshing does not clear tool approval, posting, or other task blockers.
Unsupported question types stay read-only. Visible application questions refresh
every five seconds and on app activation. Drafts survive sync transitions and
navigation. Canonical revisions invalidate stale review permission. Conflicting
or changed-field drafts remain under **Retained drafts** and are excluded from
submission; matching accepted answers need no second send. Drafts remain in
memory until the app quits; they are not a second question pool.

Each canonical shared question appears once in the Questions list. Application
editors show the server-projected local bindings and retain their option IDs. Identical
already-accepted submissions reconcile from a confirmed acknowledgement or fresh
server reads. Conflicting answers show the server's current value and are never
automatically replayed. An uncertain pending request still requires confirmation.
Cross-device visibility is bounded by polling, not instantaneous delivery.

Select **Instructions for agent** in a task's details to describe how to address
a blocker. The editor shows the task's current status, the effect of sending,
and its saved instruction history. New instructions replace earlier guidance.
The **Send instructions** button stays at the bottom of the editor. Drafts stay
with their task while the app runs; a failed save keeps the draft. Refresh and
review the current status after a conflict. An uncertain save uses **Check saved
request** to replay the exact saved request, including after the app restarts.

The backend controls the effect. It may schedule a browser prerequisite check,
retry preparation for the already approved application, or save guidance for
review only. A saved instruction does not mean the blocker cleared. Application
answers and consent keep their separate controls. An uncertain submission is
never retried through this editor. This feature requires the backend's
`unblock_instructions` capability; older backends keep the editor unavailable.

Eligible queued tasks and tasks waiting to retry have a cancellation action in
the task details. Cancellation does not withdraw a submitted application or stop
work that is already running.

## Local server controls

The dashboard and **JR** menu provide **Start server** and **Stop server**.
Starting can resume queued work, including approved applications. Stop unloads
the current-user `app.jobreview.backend` LaunchAgent. It sends the backend through
its existing graceful shutdown and removes its `KeepAlive` registration.
The app waits for the process and listener to disappear before reporting **Stopped**.

Controls use the existing `~/Library/LaunchAgents/app.jobreview.backend.plist`.
They require a matching local private connection, credential fingerprint, known
program arguments, and safe file ownership. A remote connection, missing setup,
or conflicting process/port disables the controls. Local process status is
independent of HTTPS authentication and network reachability. A loaded agent
without a ready listener shows **Starting or waiting**, not **Running**.

Start and Stop disable repeat clicks while pending. Failed commands show their
result and refresh the actual local status. **Refresh status** makes no changes.
Closing or quitting the app leaves the server unchanged. LaunchAgent settings
are preserved, so the installed login behavior still applies after a new login.
`DashboardProbe --local-server-status` checks this state without an HTTP request
or any lifecycle command. It is safe to use while the server must remain stopped.

Version 1.7.2 also recognizes the deployed `.venv-pool/bin/python` environment
and the `--no-workers` API-only mode. It validates environment ownership and
permissions and preserves the exact LaunchAgent arguments. Starting an API-only
installation keeps workers paused; Start does not remove `--no-workers`.
Unknown interpreters, extra arguments, foreign connections, and unsafe files
still disable local controls.

`DashboardProbe --attention` reads the current attention items and each supported
instruction endpoint. It reports blocker codes, answer counts, and instruction
modes. It never writes to the backend or the app's saved drafts, and prints no
answer values or instruction text.

If the Mac is locked and unsent drafts cannot be checked, use
`python3 job-review-mac/scripts/install.py --defer-relaunch` to install on disk
while preserving the running frontend. The new controls appear after the app is
reopened. Check or preserve unsent drafts before that step.

## Connection

On this workspace, the app discovers the existing private files at
`../job-review/state/device/tailscale-access.json` and `tailscale-credential.txt`.
It searches executable/current-directory ancestors and then
`~/ml-workspace/job-hunting`. It reads the configured HTTPS origin and uses the
credential for authenticated workflow reads, explicit commands, and the project
permission agreement. It does not copy the credential into the app, a URL,
command arguments, UserDefaults, or the local workflow journal.

An explicit `~/.config/job-review-mac/connection.json` overrides discovery:

```json
{
  "origin": "https://your-mac.example.ts.net",
  "credential_file": "/absolute/path/to/private-credential.txt"
}
```

Both the explicit configuration and the existing credential file must be regular
files owned by your account, with mode `0600`. Symlink files are rejected. The
credential remains in its existing private file. An invalid explicit config is
reported as an error; it does not silently switch to another backend. Origins
must use HTTPS with no username, password, path, query, or fragment. HTTP loopback
is supported only by explicit test injection in debug builds.

## What the dashboard means

- The shared `WorkflowSession` is the authoritative source for both displayed
  data and commands. Its capabilities, feed, health, tasks, processes, and
  questions are separate reads, not a single database transaction.
- A failed endpoint read retains that endpoint's saved data and reports the
  failure. Successful endpoints still update. The dashboard marks incomplete or
  offline updates as **Updates paused** when saved data is available. With no
  saved data, a failed connection shows **Disconnected**.
- Data becomes stale after 45 seconds without a successful workflow refresh.
  New commands need a current connection and successful reads of their required
  endpoints. A failed unrelated health read does not replace fresh job data.
- Running counts use observed `running` tasks. The API does not expose worker
  thread heartbeats. A zero count does not assert that a worker process is alive.
- Chrome and LinkedIn are checked when Explore or Apply needs the browser.
  `idle` and `on_demand` are normal. Ranking uses stored data.
- Task state and submission state remain separate. Only a confirmed submission
  shows recruiting progress. An uncertain submission requires operator review.
- Application activity comes from saved process events. Discovery activity shows
  each task's latest update; an update can be a heartbeat, not a new milestone.
- The Dashboard explains failures, quarantine, scheduled retries, and unanswered
  questions. The task inspector provides the supported instruction, answer, and cancellation
  actions when the current backend state allows them.

The Mac workflow transport permits GET requests only to `/api/v1/capabilities`,
`/api/v1/feed`, `/api/v1/health`, `/api/v1/tasks`, `/api/v1/processes`, and
`/api/v1/questions`, plus `/api/v1/unblock-instructions` with exactly one
`task_id` query field. It permits POST requests only to `/api/v1/decisions`,
`/api/v1/answers`, `/api/v1/tasks/cancel`, and `/api/v1/unblock-instructions`.
A separate consent client permits
GET and POST on `/api/v1/project-consent`. Commands require the saved bearer
credential, exact Origin, and a current server CSRF token. The clients block
redirects, retain certificate validation, and disable cookies and HTTP response
caching. CSRF tokens stay in memory and are excluded from persisted models.

The app does not read or write the backend SQLite database or control Chrome
directly. Explicit Start and Stop operate only the existing local LaunchAgent.
Approved application work runs in the backend. Feed
reads can publish eligible cards as defined by the backend; only explicit
decisions consume or review cards.

## Private storage and interrupted saves

The Mac saves an origin-specific workflow journal under
`~/Library/Application Support/Job Review`. The directory must belong to your
account and use mode `0700`; journal files must belong to your account and use
mode `0600`. Symbolic links are rejected. The journal contains saved workflow
data and any pending request, including draft answers being sent. It contains no
bearer credential or CSRF token. Saved data is available for review while offline;
it does not enable new offline commands.

Before sending a command, the shared session saves its exact request durably.
If the reply is lost or uncertain, the app keeps the pending request and blocks
new commands. Use **Check saved request** to retry the same request ID, revisions,
and body against the same server origin. A refresh alone does not prove that the
command was accepted. The app will not switch server origins while a request is
pending. A failure to save the private journal prevents the command from being
sent.

## First-use agreement

A configured service that has no saved project agreement opens a native **Agree**
sheet. It covers reading LinkedIn, company websites and their application pages,
plus uploading the approved resume for approved jobs. **Not now** closes the sheet
without saving anything. Browser work stays paused; **Connection → Review
permissions** can reopen the sheet.

The service stores the agreement. Each launch and refresh reads its current state,
so an existing agreement does not prompt again. The app shows **Agreed** only after
a valid server response. An agreement already granted in the user's project chat
can be recorded by the backend operator without requiring another click.

Alongside the workflow journal, the app stores an outstanding Agree intent
(service origin, policy version, and request UUID) in a separate private file under
`~/Library/Application Support/Job Review`.
If a save response is lost, the next connection first reads the service and then,
if needed, retries the same command. That file is never treated as a saved grant
and contains no access token. A pending intent never transfers to another origin.

The agreement does not approve an unselected application, accept employer legal
terms, or override Chrome, macOS, or tool permission checks. Separate sign-in or
system actions can still be required.

## Verification

Run focused tests:

```sh
python3 job-review-mac/scripts/verify.py
```

Run a fictional demo without loading a credential or making network calls:

```sh
open -n "job-review-mac/dist/Job Review.app" --args --demo
open -n "job-review-mac/dist/Job Review.app" --args --demo --scenario=idle
open -n "job-review-mac/dist/Job Review.app" --args --demo --scenario=stale
open -n "job-review-mac/dist/Job Review.app" --args --demo --scenario=disconnected
open -n "job-review-mac/dist/Job Review.app" --args --demo --scenario=loading
open -n "job-review-mac/dist/Job Review.app" --args --demo --page=browse --scenario=browse-multiple
open -n "job-review-mac/dist/Job Review.app" --args --demo --page=board --inspect-task --scenario=questions
open -n "job-review-mac/dist/Job Review.app" --args --demo --scenario=consent-first-use
```

The active demo includes failed, queued, running, retry, and unanswered-question
states. `--ui-smoke --smoke-report=/absolute/report.json`, combined with `--demo`,
checks the real window and menu target. It leaves the demo open for inspection.
`python3 job-review-mac/scripts/smoke.py` runs 47 fictional states, renders the
app's own views, and verifies close/reopen, single-window reuse, and frontend quit.
It includes dark mode, collapsed navigation, compact windows, and search
selection changes through matching, empty, and cleared results.
This does not capture the screen or other apps. A locked Mac cannot establish
foreground focus; that check is reported separately.
Using `open -n` deliberately starts another demo process; normal app opening reuses
the existing instance.

For an explicit read-only native production check, run the built `DashboardProbe`
executable. It uses the same connection configuration but retains the narrow,
read-only `DashboardClient`: five GET endpoints, no workflow commands, and no
workflow journal. It prints only counts, statuses, and a timestamp, never a raw
API response. See
[VERIFICATION.md](VERIFICATION.md) for completed checks and limits.


## Application workflow observations

Choose **Workflow** in the sidebar or **Application Workflow** (⌘3) in the app
menu. The native diagram shows each of the three worker slots, its application,
reported stage, and state. Paused, available, and ready slots remain distinct.
The shared section shows lock owner/waiters, pending and answered Markdown counts,
the published snapshot revision/digest, and the answer handler's current stage.
Select a worker for times, question IDs, and application references.

When the backend reports `browser_access`, Workflow also shows the single shared
Chrome connection, its owner, and the first-in-first-out waiting order. A worker
at `waiting_for_browser` shows **Waiting for Chrome**, even while its lease is
running. Browser holds and uncertain work stay visible. An older server without
this additive object shows **Not reported**; the client does not infer that Chrome
is free. The Mac cannot acquire or release the connection from this view.

The Mac uses an independent read-only client. On every read it first checks
`GET /api/v1/capabilities`; only `application_workflow: true` permits
`GET /api/v1/application-workflow`. Both reads require complete v1 envelopes.
Polling runs every five seconds while the page is visible. Old observations,
failed reads, clock skew, unknown stages, and unsupported servers stay explicit.
The diagram never infers completed steps or animates progress between reads.
It does not read local Markdown files or control workers.

The API reports a stored manifest hash and worker stages; it does not report
each worker's cached digest or last comparison result. The detail pane makes
that limit explicit. Answer formatting happens before the shared lock; saves,
publication, blocker checks and worker wake-ups are code operations.

Fictional preview: add `--demo --page=workflow --scenario=workflow-publishing`.
Other samples include `workflow-idle`, `workflow-stale`, `workflow-unknown`,
`workflow-unsupported`, and `workflow-disconnected`. Samples never advance on
a timer. `DashboardProbe --application-workflow` performs only capability-gated
reads and prints status/counts, without answer text, private paths, or credentials.


The Connection page shows **Paused** when the server reports
`worker_status: paused`, including API-only mode with `worker_enabled: false`.
An absent worker configuration still shows **Not configured**. This label is
separate from local server process status and the Workflow slots' observed stages.
