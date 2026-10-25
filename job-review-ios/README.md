# jobswipe for iPhone

A native SwiftUI app for the continuous job-review feed. It targets iPhone on iOS 17 or later. It starts with nine fictional jobs, so you can review the design without touching saved job choices.

The installed app is named **jobswipe**. The Xcode project and scheme remain `JobReview`. Existing bundle identifiers, saved data, and Keychain storage keep their original identifiers.

Source export of the iOS app and shared ReviewCore package. Local installation and device history are excluded.

Automatic signing uses the configured Personal Team. The current device profile expires October 21, 2026, at 8:54 PM Pacific and needs renewal through Xcode. The Python server runs on the Mac and is reached privately through Tailscale HTTPS. Sample-mode testing does not require a live server.

## Versioned backend workflow

When the server advertises API v1, jobswipe shows **Review**, **Progress**, and **Questions** alongside the existing **Saved** and **Queue** tabs. Sample mode keeps the original manual review flow. A discovery 404 selects the legacy API. Network, authentication, and malformed-response errors do not select it.

- Select exact jobs and tap the **checkmark** to authorize those posting versions. The button shows the selected count beside the checkmark when at least one job is selected; zero is hidden and the button is disabled. Its accessible label is **Apply** with the selected count. Review decisions save immediately and show the next company after acknowledgement, without a confirmation screen. A failed or uncertain save keeps its error and recovery controls. The acknowledgement confirms authorization; it does not confirm employer submission.
- **Find more** requests another ranking or research pass. The **X** excludes the company. Each decision consumes the displayed card.
- Each company page shows its name, a logo, and up to two sentences about its business. These use optional `company.data.logo_url` and `company.data.description`; a missing or failed logo shows company initials, and missing descriptions are labeled. Logos require HTTPS without URL credentials. The ranker's selection summary is separate under **Why these jobs**.
- Browse up to three job postings with a horizontal swipe. Page dots show the current position and expose an adjustable **Jobs** control for screen readers. Selections persist across pages; the circle beside each job title shows its selection. Swiping only browses; the bottom buttons save the company decision. The next company starts with no selections.
- The selected layout is **Swipe between jobs**. Each job page includes pay, location, work setting, full supplied role text, requirements, fit, and gaps. Requirements use the posting's explicit requirements field; older feeds with no requirements show an unavailable message. **Posting & sources** opens the original link and supporting evidence.
- Each job uses the ranker's reasons and Work/Fit/Gap text when available. Original titles and posting links stay unchanged. Older cards use their imported brief.
- **Progress** shows application submission, recruiting status, tasks, receipts, and event history separately. Only submitted applications show recruiting status. Cancel is available for eligible queued tasks and tasks waiting to retry.
- **Questions** shows the exact current application questions. Enter text or choose the supplied options, then review the answers before saving. Answers apply only to that application. A changed question or form requires a new review.
- When the backend reports that its worker is not configured, authorized work stays queued. A configured worker is separate from browser health. Follow each task's status and errors in Progress.
- **Saved** and **Queue** retain their original manual handoff behavior. Historical approvals do not become Apply authorization.

## Instructions for blocked tasks

When `capabilities.unblock_instructions` is true, open **Progress → Instructions for agent** on a stopped task or its application detail. Enter up to 4,000 characters and tap **Send instructions**. The sheet shows the exact task, current blocker, and the effect the server allows before sending. New guidance replaces earlier guidance for this task; the full history remains visible with current, replaced, or no-longer-current status. Closed tasks keep read-only history.

The acknowledgement distinguishes a scheduled prerequisite check, a scheduled preparation retry for the already approved application, and guidance saved without restarting work. It never claims that the blocker cleared or that an application was submitted. Required answers stay in **Questions**, and permissions/consent keep their existing controls. Guidance for an uncertain submission cannot authorize a new attempt.

The client reads the current task immediately before sending and rejects a changed target. A conflict or connection failure keeps the draft in the open editor. Read failures keep the last loaded history visible and disable Send until a successful refresh. An uncertain write keeps its exact request on disk; **Check pending save** replays that same request ID and body. The pending text is restored when the task editor reopens. Unsent drafts are local view state and are not restored after dismissing the editor or changing servers.

The backend contract is [UNBLOCK_INSTRUCTIONS_CONTRACT.md](../job-review/UNBLOCK_INSTRUCTIONS_CONTRACT.md). An absent capability keeps the entry hidden and prevents calls to the new endpoint. The backend deployment is verified, and the shared Swift client passed live capability, task-list, and instruction-history reads. No production instruction was sent.

**Progress → Service checks** shows storage, Chrome, and LinkedIn checks. Chrome and LinkedIn are checked when a research or application task needs them. **Checked when needed** is an idle state, not a connection failure. Ranking can run without Chrome. The configured worker and each task's status are separate from browser health; a passed check does not mean an application was sent. A passed result expires at the server deadline. Offline, failed, expired, or missing health reads do not claim readiness. Older v1 servers with extra checks or without the health route remain supported. Health does not block saving decisions or answers.

The app polls every 20 seconds while active. A pending decision freezes its review card while independent progress and question reads continue. Endpoint failures leave that endpoint's cached copy visible and block actions that need current data from it.

## Manual review flow (sample or legacy server)

- One company card shows up to three job postings as horizontal pages, with exact titles, locations, work, fit, and gaps.
- Swipe to browse. Select jobs, then tap the bottom **checkmark** to approve. No jobs start selected.
- Tap a job title or its selection circle to select it. A selected row has a light purple tint. The active action shows a spinner until the result is known; its accessible label announces saving.
- Tap the **X** to exclude the company.
- Tap **Find more** to request more research at that company.
- Open **Posting & sources** for full evidence and posting links.
- Open **Saved → Ready**, select jobs, and confirm the exact queue selection.
- Saved jobs are grouped by company. Search keeps your selection and shows how many selected jobs are outside the current results. **Clear selection** removes all checks.
- Refresh keeps selected jobs that remain Ready at the same version, including when other jobs arrive or change order. Changed or unavailable selections are removed with a short message. New jobs always start unchecked.
- Open **Queue** to read application and research status, including requests for your input.

Queue confirmation keeps the exact job records and versions you reviewed. A changed version requires a new review. A failed or uncertain save leaves the confirmation open with its error and recovery action. Queue shows failed work and changed postings by default. **Full history** opens the original status log; the latest message stays visible above it.

The app saves each decision before showing the next company. Approval and queueing do not send employer applications. Return to the chat to ask the agent to process queued work. Unchecked jobs remain in **Saved → All jobs**.

Changing the data source clears unsaved selections, navigation, and open queue confirmations. Refreshing or updating credentials for the same normalized server origin keeps that view state. Unsaved selections are held only in the current app view; they are not saved decisions or drafts restored after an app restart.

## Open the iOS project

1. Open `JobReview.xcodeproj` in Xcode 16 or later.
2. Select the `JobReview` scheme and an iPhone simulator with iOS 17 or later.
3. Run the app. Sample mode is on by default.
4. For a physical iPhone, connect and pair the phone, enable Developer Mode, and select it as the destination. **Signing & Capabilities** already uses automatic signing and the Personal Team on this Mac.

From `job-review-ios/`, an unsigned simulator build can use:

```sh
xcodebuild -project JobReview.xcodeproj -scheme JobReview \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/iOS CODE_SIGNING_ALLOWED=NO build
```

Use the normal Xcode Run action, or omit `CODE_SIGNING_ALLOWED=NO`, for backend connection testing. The simulator needs local signing for Keychain access. An unsigned build can compile and show sample data but can fail to connect with Keychain error `-34018`. Local simulator signing does not require device provisioning.

This command needs full Xcode, the iOS SDK, and completed Xcode license setup. See `VERIFICATION.md` for the checks that ran here.

For a direct tool check from the workspace root:

```sh
python3 job-review-ios/scripts/check_ios.py
```

The check requires Xcode 16 or later and an iOS Simulator SDK of 17 or later. It returns exit code `2` when a requirement is missing. It does not install tools or change the selected developer directory.

After Xcode is installed, use `--build` to run the unsigned simulator build. If Command Line Tools remain selected, pass the installed Xcode directory for this command only:

```sh
python3 job-review-ios/scripts/check_ios.py --build \
  --developer-dir /Applications/Xcode.app/Contents/Developer
```

A simulator build does not need an Apple signing team. Running on an iPhone needs your team, device pairing, and Developer Mode. See [Apple's device setup guide](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices). Simulator runtime installation and actual launch checks remain separate from this build check.

## First physical iPhone installation

The project uses automatic signing with a signing team selected locally in Xcode and bundle ID `app.jobreview.personal`. The steps below apply to a first installation on another device; an update on the current phone preserves its app data and Keychain entries.

1. Connect the iPhone to this Mac by USB. Unlock both devices.
2. If the iPhone asks **Trust This Computer?**, choose **Trust** and enter the phone's passcode on the phone. Let Xcode's Device Hub finish pairing.
3. On the iPhone, open **Settings → Privacy & Security → Developer Mode**. Enable it and complete the restart and confirmation prompts. See [Apple's Developer Mode guide](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device).
4. From the workspace root, run the read-only check below. Copy the exact physical iPhone ID from its output.

```sh
python3 job-review-ios/scripts/install_iphone.py
```

Run the install command with that ID:

```sh
python3 job-review-ios/scripts/install_iphone.py --device '<exact physical iPhone ID>' --install
```

The helper rejects simulators, ambiguous IDs, and phones that are not connected and paired. It uses the signing team you selected locally, lets Xcode register the device and create its development profile, builds the app, verifies its signature and bundle ID, then installs and launches it. Each failure stops later steps. If Xcode requires Apple Account sign-in, complete that in Xcode and rerun the command. Running the helper without `--install` never provisions, builds, or installs anything.

The first launch uses sample data. To connect after the backend's private HTTPS deployment is ready:

1. Open jobswipe **Settings** and turn off **Use sample feed**.
2. Enter the exact deployed HTTPS origin in **Review server**. It has no API path. If the deployment uses a private network, connect the iPhone to that network first.
3. Enable **Replace access token**.
4. Enter the raw device credential from the backend's private credential file in **Access token**, without a `Bearer ` prefix. Use local entry; do not put the credential in a command, URL, screenshot, log, or app bundle.
5. Tap **Connect**. The app saves the credential in this iPhone's Keychain, clears the entry field, and loads current data. A later same-origin credential replacement retains pending requests.

After connection, check Review and Progress before making real decisions. **Worker checks** will still report the deferred agent adapters as not configured. API connectivity does not mean those workers can execute applications.

## Connect to data

In **Settings**, turn off **Use sample feed** and enter the server origin. A server origin has no path, query, username, or password. Remote origins must use HTTPS. HTTP is allowed only for loopback development.

The default development address is `http://localhost:8766`. It can reach the Mac server from an iOS simulator on that Mac. On a physical iPhone, `localhost` means the phone; it cannot reach the Mac server. The server always binds to `127.0.0.1`. Its default mode accepts only local hosts. Device access mode also accepts its configured HTTPS host through a gateway.

The client implements `../job-review/CONTRACT_V1.md` and retains `../job-review/CONTRACT.md` for manual operations:

| Workflow operation | Endpoint |
| --- | --- |
| Discover capabilities | `GET /api/v1/capabilities` |
| Read startup checks | `GET /api/v1/health` |
| Load cards, applications, questions, tasks | `GET /api/v1/feed`, `/processes`, `/questions`, `/tasks` |
| Save an explicit card decision | `POST /api/v1/decisions` |
| Save versioned answers | `POST /api/v1/answers` |
| Cancel eligible work | `POST /api/v1/tasks/cancel` |

| Operation | Endpoint |
| --- | --- |
| Load current state | `GET /api/state` |
| Save a complete card | `POST /api/card` |
| Queue an explicit subset of approved jobs | `POST /api/queue` |

Writes use the server's session token and matching Origin header. In device access mode, every read and write also requires a bearer credential. In Settings, enable **Replace access token**, enter that credential in **Access token**, and connect to the configured HTTPS origin. The app stores the credential in the device Keychain. A token is optional only for the default loopback mode.

## Private iPhone access

The backend's opt-in `--access-config` mode checks bearer authentication on every API read and write. The session token still protects writes; it is separate from the device credential. See the [backend setup steps](../job-review/README.md#iphone-access-away-from-home) for credential provisioning and private HTTPS deployment.

The backend includes a separate Caddy HTTPS gateway that forwards traffic to the loopback server running in device access mode. The app keeps certificate validation enabled and sends the configured HTTPS Origin unchanged. A `remote_access: true` capability confirms server configuration only; it does not prove gateway deployment or iPhone connectivity. The deployed origin, trusted certificate, network route, and physical iPhone connection need their own verification.

## Offline use and recovery

The app caches the latest feed on the device. Offline data is read only. Refresh after connecting before making a new decision.

Before sending a decision, the app writes its exact JSON bytes, request ID, and server origin to protected local storage. It allows one pending write at a time. If the response is lost, **Check pending save** sends the same bytes to the same server. A refresh does not advance an unresolved card. The server's idempotency check prevents duplicate work.

A v1 conflict marked `stale_revision` or `operation_rejected` clears the rejected attempt, loads current data, and requires a new review. A reused request ID or unknown conflict reason keeps the exact pending request. A successful acknowledgement is saved before any follow-up reads; a failed read cannot send a new Apply. An unresolved legacy command must be recovered before switching to v1.

Plain-text gateway errors, including HTTP `411`, `413`, `421`, and `502`, keep the pending request. After the connection or gateway is repaired, recovery sends the original request ID and body to the same server.

A legacy revision conflict loads current data and requires a new review. Auth failures, rate limits, timeouts, and unknown outcomes keep the pending request. You can replace credentials for the same server in Settings while a save is pending. You cannot move that request to another server.

Caches are separate for sample mode and each normalized server origin. They contain no session or bearer tokens. On iOS, cache files use device file protection and are excluded from device backup. A corrupt cache reports an error and stays in place for recovery. Removing the app removes its local cache; server choices stay on the server.

## Code and checks

`Sources/ReviewCore` contains the API models, request rules, transport, persistence, and single-writer session. `App` contains the SwiftUI screens and Keychain integration. `Tests/ReviewCoreChecks` is a dependency-free Swift test runner. It avoids the test-runtime gap in this Mac's Command Line Tools.

From the workspace root:

```sh
python3 job-review-ios/scripts/verify.py
```

This builds all shared SwiftUI source for macOS and runs the core checks. It also starts separate temporary loopback Python servers with fictional data, then verifies legacy card/queue operations and versioned commands against them. It never opens the production database or contacts employers. Use `--no-integration` only when loopback binding is unavailable; that omits the HTTP contract check.

For the real Swift client through Caddy HTTPS:

```sh
python3 job-review-ios/scripts/verify_https.py \
  --caddy job-review/.tools/caddy/2.11.7/caddy
```

This separate target runs the production API and session code through URLSession, real Caddy, and an authenticated backend with fictional temporary data. A test-only transport trusts one temporary CA while validating hostname and certificate dates. The production transport must reject that CA. No CA is installed in the Mac or simulator, and no test trust code is linked into the app. The checks exercise authentication, durable Apply recovery after a real committed response is lost, exact replay, typed answers, cancellation, HTTP protocol metrics, and backend storage effects. Services and temporary files are cleaned up on completion or failure. A missing Caddy binary fails explicitly.

The install helper's focused checks run with:

```sh
python3 -m unittest discover -s job-review-ios/scripts -p 'test_install_iphone.py'
```

For a local desktop preview after a successful build:

```sh
python3 job-review-ios/scripts/package_preview.py
```

The script creates `.build/jobswipe Preview.app`. It does not launch or install it. This is a macOS preview of shared views, not an iOS simulator.

## Current limits

- The iPhone 17e simulator passed the sample review, detail, selection, search, exact queue, and relaunch checks. See `VERIFICATION.md` for the tested scope. Physical-device provisioning, VoiceOver, Reduce Motion, and authenticated remote access remain unverified.
- The review action bar stacks at large text sizes. Saved selection controls scroll with the job list at accessibility sizes, so they cannot consume the list's full height. Approval stays disabled until a job is selected.
- The app uses a warm light theme in this first version.
- Per-job note editing and restoring excluded companies remain in the web app. Eligible v1 work can be cancelled in Progress; the legacy queue stays a manual handoff.
- The v1 worker and external execution adapters are not configured. New research and application execution need that separate backend work.

## Fictional simulator backend

Run `python3 job-review-ios/scripts/simulator_fixture.py` from the workspace root. It prints a loopback URL. Turn off the sample feed in app Settings and enter that URL. The temporary server includes two available company cards and one fictional application with text, single-choice, and multiple-choice questions. Stop it with Control-C when finished; its temporary database is removed. This helper never opens the production database.
