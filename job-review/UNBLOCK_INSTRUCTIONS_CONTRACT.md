# Task unblock instructions — API v1

Backend contract updated October 20, 2026: recurring browser monitoring is removed. Clients must require `capabilities.unblock_instructions == true`; absent means unsupported.
This adds no new job approval, legal consent, question answer or submission retry.
Sending instructions can request one bounded preparation retry for the existing
approved application when the host confirms it is safe. This is not a submit action.

## Read

`GET /api/v1/unblock-instructions?task_id=<URL-encoded task ID>`

Require exactly one `task_id` query field. The response uses the existing complete
v1 read envelope: `schema_version: 1`, content `revision`, `complete: true`,
`next_cursor: null`, and current `token`. Its data fields are:

```json
{
  "target": {
    "task_id": "fictional-task",
    "task_updated_at": "2026-10-19T20:00:00.000000+00:00",
    "task_status": "waiting_for_user",
    "process_id": "fictional-process",
    "process_revision": 7,
    "block_reason": "tool_approval_required",
    "can_submit": true,
    "mode": "manual_review",
    "unavailable_reason": null
  },
  "instructions": []
}
```

Explore tasks have null `process_id` and `process_revision`. `instructions` is
the full history for this task, oldest first. Each entry has `id`, `request_id`,
`task_id`, `process_id`, `process_revision`, `task_updated_at`, `text`, `mode`,
`wake_status`, `created_at`, and `status`. Status is `current`, `superseded` by
newer guidance, or `stale`. Only the latest valid instruction reaches runtime
context. A new instruction replaces the earlier guidance; show this in the UI.
Text must render as plain text, not executable HTML or Markdown actions.

The target's `can_submit` controls the editor. Unavailable reasons include
`task_busy`, `task_not_blocked`, `task_closed`, `authorization_changed`, and
`stale_revision`. An active task can temporarily make the target unavailable.
Refresh after it stops. Read history remains available for closed tasks.

## Write

`POST /api/v1/unblock-instructions`

Use the same device authentication, exact Origin and `X-Review-Token` checks as
existing v1 writes. Send exactly these six fields, using the latest read target:

```json
{
  "request_id": "client-generated-UUID",
  "task_id": "fictional-task",
  "task_updated_at": "2026-10-19T20:00:00.000000+00:00",
  "process_id": "fictional-process",
  "process_revision": 7,
  "text": "I completed sign-in in the existing browser tab. Please check it."
}
```

Text must contain 1–4000 characters and cannot be whitespace only. Keep null
process fields for Explore. Do not send authority, permission or consent fields.
The request checks the exact task timestamp, process identity and integer process
revision in one transaction. It saves an immutable history row and updates the
task timestamp. Concurrent stale writes return `409` with `stale_revision`.

Success is HTTP 200 with the usual `schema_version` and current `token`, plus:

```json
{
  "request_id": "client-generated-UUID",
  "accepted": true,
  "instruction_id": "server-generated-UUID",
  "task_ids": ["fictional-task"],
  "process_ids": ["fictional-process"],
  "mode": "manual_review",
  "wake_status": "saved_only",
  "task_updated_at": "2026-10-19T20:00:01.000000+00:00"
}
```

An Explore receipt has `process_ids: []`. Preserve the exact request body and ID
until the outcome is known. Replaying an accepted request returns its original
receipt even after task state changes. Reusing its ID with a different payload
returns `409 request_id_conflict`. An uncertain transport/HTTP outcome requires
retrying that same request unchanged. After a definite stale conflict, refresh
and require the user to review the new target before sending a new request ID.

Other errors use the existing v1 envelope: `400 invalid_request`, `401 unauthorized`,
`403 forbidden`, `404 not_found`, or `409 conflict` with `error.reason` such as
`task_busy`, `task_not_blocked`, `task_closed`, or `authorization_changed`.
No request failure implies that a browser action occurred.

## Modes and wake behavior

| Mode | Receipt `wake_status` | Effect |
| --- | --- | --- |
| `preparation_retry` | `retry_scheduled` | Queues the same approved pre-submit task for ordinary readiness and preparation. Preserves its retry budget and current blocker evidence; does not claim the blocker cleared. |
| `questions_pending` | `saved_only` | Retains guidance. Required answers still use `/api/v1/answers`; this text is not a structured answer. |
| `reconciliation_only` | `saved_only` | Retains guidance for the uncertain attempt. No readiness retry, requeue or submission replay. |
| `manual_review` | `saved_only` | Retains guidance for another stopped blocker, quarantine, unavailable consent or exhausted budget. No execution change. |

New writes never return `browser_recheck` or `recheck_scheduled`. Older receipts
retain their historical mode for exact idempotent replay; replay starts no work.
There is no recurring browser-monitor scheduler.

`preparation_retry` is limited to stopped preparation with `tool_approval_required`,
`file_access_required`, `browser_action_required`, `application_action_required`,
or `application_page_transient`. It requires current exact-job authorization,
project consent, remaining retry budget, no required unanswered questions, no
active task lease or quarantine, and no submission checkpoint or recorded attempt.
Normal readiness, browser/tool approvals and final-submit checks still apply.
An instruction with missing required answers is saved only. Login, challenge and
other ineligible waits remain parked for manual follow-up. The UI must
show the returned mode and wake status rather than promise an application resume.

## Runtime and migration integration

`store.unblock_instruction_context(task_id, token, purpose=...)` is read-only and
requires the current live task lease. Supported purposes: `readiness`, `application`, `exploration`, `reconciliation`. It returns null or the latest
instruction with task/process identity, text, creation time, mode, user authority
and a fixed host boundary. Replaced instructions, changed process revision,
changed form/blocker identity, changed approved snapshot, and changed Explore
payload or blocker reason do not enter new turns. The one exception is
`preparation_retry`: guidance survives its own ordinary `start_application`
revision increment only when the immediately preceding process event is this
instruction and the latest event starts preparation of this exact task. Any
answer, new blocker or other revision change ends that exception. Routine lease heartbeats
and Explore checkpoint bookkeeping do not expire guidance. Application authorization is rechecked.
Uncertain attempts expose guidance only for explicit reconciliation purpose.
The deployed runtime includes fresh guidance in readiness, exploration, application
preparation prompts. Final submission and structured answer
review do not consume free-text guidance.

`UNBLOCK_SCHEMA` in `unblock_instructions.py` is an additive, idempotent table and
index migration. `WorkflowStore` includes it in normal initialization. Deployment
must back up the stopped database before constructing the new store or executing
this schema. This deployment backed up the stopped database and preserved all
application records. See `RUNTIME_VERIFICATION.md` for deployment evidence.

Verification: 19 focused instruction tests and 130 related backend tests passed
(the 19 tests are included in the 130). Coverage includes authenticated local
HTTP, idempotence and stale writes, explicit leased guidance, one normal
preparation retry, lease/revision/approval fences, and preserved question and
submission gates. All test databases and instructions are fictional.

The complete deployed backend passed 737 tests. Production verification used only
GET requests; no instruction or application was submitted for testing. The installed
Mac 1.5.0 build 7 editor passed a live read. iOS build 12 is installed; physical
launch awaits the user unlocking the phone. The built iOS client also passed live
GET-only capability, task and instruction history checks. Both clients passed fictional save,
history, draft preservation and request-replay checks in their own test suites.

October 20 removal verification: 678 full backend tests passed without skips.
Live authenticated reads passed after restart. New writes never schedule browser
monitoring. The deployment preserved all application records and quarantine.
