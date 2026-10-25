# Cross-device question and answer synchronization

User requirement, October 23, 2026: iOS and Mac show the same shared question
state. Answering on either device must update the other; the user must not be
required to answer an equivalent, already answered question again.

## Ownership and contract

- Backend sub-agent owns durable question identity, linking, fan-out, revision
  checks, idempotent writes, and fictional two-client API tests.
- Existing Mac chat owns Mac UI and tests.
- Coordinating parent owns required iOS/shared ReviewCore changes and tests.
- Both clients read the server. Their local caches/drafts are not a second pool.
- Preserve old process-scoped v1 APIs. Add shared question identity without
  changing existing IDs, option values, or consent scopes. Confirmed additive
  fields are `pool_question_id` (nullable string) and `pool_revision` (integer).
  Old servers may omit both. Client drafts use their local question revision,
  not global pool revision, to determine whether that binding changed.
- Existing answer acknowledgements keep `process_ids` and `task_ids` scoped to
  the addressed original process/task. Additional fan-out information must use
  additive fields; old clients reject an unexpected multi-task acknowledgement.
  Optional `already_answered` reports an equivalent accepted answer;
  `affected_process_ids` and `affected_task_ids` can report compatible fan-out.

## Required behavior

1. One committed answer resolves all compatible linked application questions.
   Each binding keeps its own form/option IDs and scope. Publish pool/answer
   revisions so a stale snapshot cannot overwrite newer accepted answers.
2. Both apps refresh on foreground/reconnect and while questions are visible.
   A remote answer becomes saved/read-only rather than an empty answer field.
3. Remote changes remove only invalidated/resolved draft entries. Preserve
   unrelated unsent answers. A changed form invalidates its affected bindings.
4. A stale confirmation cannot submit over a remote answer. Show current saved
   answers and preserve remaining drafts. Corrections require explicit review of
   the current revision; never automatically replay a conflicting stale draft.
5. Identical duplicate submissions are idempotent. A second-device stale request
   that matches accepted answers can reconcile as already answered; it does not
   create another answer history entry, worker wake, or application submission.
6. Offline or incomplete reads cannot claim current synchronization or success.
   Preserve pending commands and drafts. Reconnect refreshes before new actions.
7. Foreground polling provides bounded eventual visibility, not instantaneous
   cross-device delivery. Server revision checks enforce correctness between polls.

## Verification

Test Mac answer -> iOS refresh; iOS answer -> Mac refresh; simultaneous identical
answers; conflicting stale answers; partial application answers with unrelated
drafts; changed form revisions; restart/reconnect; and mapped option IDs across
equivalent forms. Use fictional data and temporary servers. Never answer a real
employer question solely to test synchronization.

## Canonical snapshot extension (October 25, 2026)

`capabilities.question_sync` is true when the shared pool is configured. Clients
then use `GET /api/v1/question-sync` in place of separate question/process reads.
The standard v1 envelope contains a `question_sync` object:

- `sync.state`: `ready`, `updating`, or `pending`.
- `sync.revision`: a durable, nondecreasing synchronization signal. It combines
  the existing lock generation and pool revision. Polling does not change it.
- `sync.pool_revision`: the canonical pool business revision, used for writes.
- `shared_questions`: all canonical shared question records, including saved
  knowledge that has no current form binding. Fields: `id`, `text`, `type`, `choices`, `required`, `scope`,
  `status`, `answer`, `answer_revision`, `answered_at`, and `binding_ids`.
- `questions`: existing form bindings, with `pool_question_id`, `pool_revision`,
  and `pool_answer_revision`. Each answer and timestamp comes from the canonical
  record, mapped to that form's choice IDs. A deferred form update cannot make
  an accepted shared answer disappear or create a competing accepted answer.
- `processes`: existing process objects from the same SQLite read transaction.

`status` is only `open` or `answered`. Sync state does not change a question's
business status or an application's gate. `answer_revision` is the latest
canonical answer history row ID; it is zero before an answer exists.
`pool_answer_revision` references that same ID. Per-form `revision` remains its
existing command fence, including when a form projection is still deferred.
All canonical records are included. Saved profile knowledge can have an empty
`binding_ids` array; it stays answered, not a new question. `questions` contains
current canonical-linked form bindings. Answered or submitted historical form
rows without a canonical link remain in the legacy audit/API and are listed in
`historical_unlinked_question_ids` (an array in ready state, null otherwise).
The new read does not choose an answer from those historical form copies.
An unlinked unanswered active form still makes the snapshot pending.

While any valid shared writer lock is held, `sync.state` is `updating`. An
expired retained lock, unpublished committed projection, or missing canonical
binding gives `pending`. All three arrays are null in either state. Clients keep
their last ready data and unsent drafts, display “Waiting for sync,” and poll.
They must never interpret null arrays as an empty question list. Existing
startup/worker recovery repairs interrupted writes; the new read does not take
or renew a writer lock. A missing binding requires backend registration rather
than client-side matching or guessing.

Clients accept ready snapshots whose sync revision is greater than or equal to
the cached revision. Process fields can change at the same pool revision. They
refresh when foregrounded or reconnected and poll while questions are visible.
This revision/read protocol handles missed updates without a push message bus.
Offline or failed reads do not establish that the client is synchronized.

`POST /api/v1/answers` accepts the additive `expected_pool_revision` integer from
the ready snapshot. The handler checks it before formatting and again under the
same shared writer lock used for the save. A stale differing command returns
HTTP 409 with `error.reason = "question_sync_stale"`. Refresh before presenting a
correction; keep unrelated drafts. Identical request replay and answers equal
to the canonical accepted value return 200 with `already_answered`, even if the
client revision is old. They do not append a duplicate answer record. Normal
form, process, consent, and application guards still apply to actual changes.

Legacy API fields remain supported. Legacy question reads also overlay
canonical accepted values. A stale legacy form copy cannot overwrite a different
canonical answer. The original `process_ids` and `task_ids` in acknowledgements
still contain only the addressed process/task. The new protocol adds no client
writeable question store. SQLite canonical records remain authoritative;
Markdown and local form answer columns remain projections.

Exact fictional wire examples: `fixtures/question-sync.json` and
`fixtures/question-sync-updating.json`. Replace `updating` with `pending` for the
same null-array pending shape. Existing endpoint authentication and device scope
apply to the new read.
