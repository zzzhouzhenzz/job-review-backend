# Continuous job review contract

This document covers the legacy web UI and unversioned API. The additive
[backend v1 contract](CONTRACT_V1.md) covers explicit Apply authorization,
durable tasks, feed refill, process tracking, and questions. Legacy Approve and
Queue actions retain the meanings below. The default v1 agent runtime is disabled.

Local Python stdlib + SQLite + dependency-free HTML/CSS/JS. Keep the existing dated research and decisions unchanged. New canonical app is job-review/. Serve at 127.0.0.1:8766. No employer requests or automated applications in this app.

## API and state

GET /api/state returns {token, companies:[], jobs:[], queue:[], imports:[], counts:{}, flashcards:[], card_reviews:[]}. Every response is current durable state except token. POST actions return the same full state. GET /api/export downloads JSON with all state except token. Use no-store. Host, Origin, X-Review-Token checks on writes. Errors {error:...}; stale entity revision -> HTTP409; UI must refetch before further edits.

Company: {id,name,data:{...original company metadata excluding jobs},excluded:boolean,research_status:'none'|'requested'|'in_progress'|'done',note,revision,first_seen,updated_at}.
Job: {key,company_id,job_id,data:{...original posting},revision,decision:'pending'|'later'|'approve'|'pass',note,first_seen,updated_at,changed:boolean}. key is company_id+':'+job_id. Exact key opaque to client. data may include brief:{work,fit,gap} for concise first view; original full evidence preserved.
Queue entry: {id,kind:'application'|'research',company_id,job_key:null|string,status:'queued'|'in_progress'|'needs_input'|'applied'|'failed'|'done'|'cancelled'|'stale',snapshot:{...authoritative job data or company data},created_at,updated_at,note}. Job queue snapshot immutable. Status and note can change through operator CLI; don't implement applying. Applied only with operator evidence note. Failed/needs_input never shown as applied.
Imports: {id,source,created_at,added_companies,added_jobs,updated_jobs}.
Flash card: {company_id,revision,jobs:[Job],remaining_jobs,newest_job_at}. revision is the company revision. jobs contains one to three current Job records. remaining_jobs counts all eligible jobs for this company, including later cards.
Card review: {id,company_id,action:'approve'|'research'|'exclude',jobs:[{key,revision,title,material_hash}],selected:[job_key],note,created_at}. id is the request ID. This is durable history, separate from application queue history. counts adds flashcards, card_jobs, and reviewed_cards.

POST /api/card {request_id,company_id,revision,jobs:[{key,revision}],action,selected:[job_key],note?}. Save one complete current card in a single transaction. The payload must include every job on that card, with no duplicates. Approve requires at least one selected key, all from the shown card. Research and exclude require an empty selected list. The server checks the company revision, exact card membership, and job revisions before writing. A mismatch returns HTTP409 with no partial save.

- approve changes only selected jobs to approve and clears their changed flag. It does not queue applications. Unselected jobs keep their decisions.
- research creates one active research request per company, or reuses the existing request. It preserves job decisions. A nonempty note adds context to the request.
- exclude hides the company, cancels queued work, and clears approvals as described below. Started work prevents exclusion.

All three actions record the shown job versions as reviewed and preserve earlier company notes. The same request ID with the same payload returns current state without repeating the action. A different payload with that ID returns HTTP409.

POST /api/job {key,revision,decision,note}. Edits one job only. Active application queued/in_progress/needs_input or applied prevents changing decision; cancel a queued item first. Excluded company cannot approve.
POST /api/company {id,revision,excluded,note}. Excluding cancels QUEUED applications and research for that company, resets research_status, keeps all past records, clears approvals except applied history, and hides the company from Inbox and Review. If any application or research is in_progress/needs_input, reject exclusion with a specific error. Restoration does not revive old approvals or erase card history. Company exclusion survives imports.
POST /api/research {id,revision,note}. Adds one research queue entry per company if none active. Sets research_status=requested. Does not clear job decisions; can approve some jobs and research more. Excluded companies must be restored first.
POST /api/queue {request_id, jobs:[{key,revision}]}. Atomic; any non-approve, excluded, changed version, or active/applied duplicate ->409/400 with no partial enqueue. Nonempty subset accepted; no need to review all. Server derives exact snapshots. Idempotency key returns existing result for same payload, rejects mismatched payload. All queued jobs keep decision=approve, UI displays Queued using queue entry. Adds no jobs implicitly.
POST /api/cancel {id}. Only queued entries can cancel. Application cancellation resets job decision to pending. Research cancellation resets research_status=none.

## Import and persistence

import_catalog.py <file.json> accepts {companies:[original company objects each with jobs]}, any positive company/job count; not fixed at 20/3. Each company has stable id; each job exact job_id and HTTPS source URL. Validate the whole import before writes. Stable key or normalized canonical URL deduplicates; conflicting company IDs/URLs fail explicitly. Add new rows, update existing research, preserve company/job choices and queue history, never delete missing jobs. Material job changes (title,url,location,arrangement,salary,level,gaps) reset approval to pending, set changed=true, mark queued snapshots stale so they cannot be applied; preserve old snapshot. Any started/applied history remains intact but operator must compare revision before use. Identical reimport is idempotent. Rank/copy-only edits do not revoke approvals. Import holds SQLite transaction and live GET reads refreshed data without server restart.

Card progress is stored by job key and material hash. A Review card can contain only pending jobs with no active/applied application, from an included company, whose current material hash has not been reviewed. New jobs and materially changed pending jobs can appear. Later and Pass remain saved choices. Rank/copy-only updates do not reset card progress. Cancelling work or restoring a company does not erase prior card history; those jobs remain available in All jobs.

Cards list companies by their newest eligible first_seen timestamp. For each company, take the newest eligible import group, sort it by rank, and show at most three jobs. Additional jobs remain for later cards. Do not insert a new job into a card already shown without requiring a fresh review of that card.

Migration command imports old data.json plus optional decisions dir once. Preserve old decisions untouched. If old draft exists migrate unqueued choices; if old submitted review exists migrate selected jobs as queued with original snapshots and other company actions. Never interpret QA files under /private/tmp as approvals. Migration keyed source must not repeat or overwrite live choices.

For draft and submitted files, compare saved_at (updated_at fallback) with submitted_at. Overlay only a strictly newer draft; invalid or missing ordering timestamps fail explicitly. Newer draft exclusions cancel old queued work but retain its snapshots. When card support is first installed, migrate earlier research history once. Mark only jobs that existed and had not changed at the time of that request as reviewed. Never mark newly imported or subsequently edited jobs as reviewed from old research history.

## UI

Preserve cream/ink/green design. Main view is Review: one centered company card with up to three compact jobs in a vertical list. Show exact linked titles, work setting, and short work, fit, and gap text. Full location, pay, original facts, evidence, and sources stay in Details when needed to keep the first view concise. Required gaps must remain clear. No checkboxes start selected. Keep the main view focused; Browse exposes navigation, filters, search, and export.

Horizontal swipes use the same save path as the labeled buttons. Left excludes the company. Right approves only checked jobs; an empty selection shows guidance and sends no request. Find more remains a separate button. A swipe must pass a distance threshold and have clear horizontal movement. Ignore vertical scroll, short movement, pointer cancellation, and starts on links or interactive controls. Require a fresh, unlocked card at commit time. Honor reduced-motion settings. Do not animate the next card into place until the server confirms the save.

Card actions are Approve selected jobs, Find more jobs, and Exclude company. Checkboxes alone do not save choices. Send only the checked keys for approval. Wait for a confirmed save before advancing to the next card. Then clear all checkbox selections. Show company and saved-card counts. Review last decision opens that company's saved jobs. Users can stop after any card; no all-companies gate exists.

Keep Inbox, Later, Ready, Work queue, All jobs, and Excluded companies as secondary views. Use search and company filters. List views render 20 rows at a time with Load more. Per-job Approve, Later, Pass, Reset, and notes remain available there. Restore excluded companies without reviving old approvals. Remember view, search, filter, and scroll locally; keep authoritative review progress in SQLite.

Ready previews exact selected jobs before Queue selected jobs records them. No implicit selections become approvals. Queue success says the work is saved and asks the user to return to chat to start. Work queue shows status and history; only queued work can cancel. Export choices includes card history.

Show save and conflict failures. After HTTP409, fetch current state and require the user to check the changed card. Keep a card or queue request with an uncertain outcome in browser storage. Check card decision or Check queue request retries its exact payload and request ID. Do not advance, claim success, or create a replacement request until the outcome is confirmed. Offline controls cannot save. Save or discard unsaved notes before another decision. Refresh feed loads imports without clearing saved choices.

## Verification

Tests for partial queue, repeated idempotency, additive imports (new company and fourth job), duplicate prevention, stable choices after imports, exclusion survives future jobs, material change invalidates queued approval, draft migration and submitted migration once, stale client write conflict, correct snapshot payload, persistence restart, status evidence enforcement. Test UI on isolated DB/port8771, never seed test choices into production. No external apply.

Card checks cover exact membership and selection, atomic save, request retry, durable progress after reload/restart, new imports after research, changed facts, and rank/copy-only updates. Verify that unselected jobs remain available without implicit approval. Test stale cards and lost-response recovery without duplicate approvals or research requests.
