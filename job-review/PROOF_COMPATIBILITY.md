# Reviewed runtime compatibility with an existing native proof

This is an operator path. It does not run a native turn or claim that changed
application code ran during the original proof. The original proof, receipts,
results, and FIFO evidence stay unchanged.

The native fixture directly exercises the app-server and Chrome FIFO gate.
Prompt/reference changes and deferred application readiness need offline
application tests. Repeating the native fixture does not test those behaviors.
The original native evidence can remain valid only through an exact reviewed
compatibility audit. Normal `install` and `resolve` remain strict when no audit
is installed. There is no HTTP or model-checkpoint installer.

## Review boundary

The runtime source manifest may name only changed files from this fixed set:

- `codex_runtime.py`
- `readiness.py`
- `application_pool_proof.py` (the reviewed compatibility adapter itself)
- `applicant_evidence.py` (pinned separately because the old proof did not name it)

These are not permanent exemptions. Each before/after hash is reviewed and the
entire effective installed identity is pinned. The review must confirm that the
runtime changes preserve action authority, exact tab affinity, denial stops,
submission guards, uncertainty handling, and verified handoff requirements.
The adapter cannot determine semantic equivalence from file names.

All other proof source bytes must stay unchanged. Host identity, native binary,
Chrome/Codex bundle identity, runtime policy, configuration identity, working
directory, timeouts, and heartbeat intervals must remain equal to the original
proof. Any unexpected change fails closed. A new native proof is a separate
operator decision, never an automatic fallback.

## Operator functions

Run `prepare_compatibility` from the isolated adapter before changing installed
source. The explicit installed root makes this possible without importing the
candidate as the deployed runtime:

```python
reference = prepare_compatibility(
    proof_file, baseline_identity,
    installed_source_root=installed_root,
    target_source_root=combined_candidate_root,
    source_manifest_file=runtime_manifest,
    patch_file=reviewed_patch,
    tests_file=test_report,
    audit_file=durable_audit_path,
)
```

Collect `baseline_identity` from the current installed runtime configuration; do
not substitute the embedded historical identity or a client-supplied object.
This step strictly validates the actual baseline and all original native
observations. It checks the exact candidate, patch and test evidence. It writes
one new owner-only audit with exclusive creation and fsync. It does not write
SQLite. The installed and candidate directories must differ. The candidate
path is not persisted and is not needed after preparation.

After the stopped deployment installs exactly those reviewed bytes:

```python
marker = install_compatibility(store, durable_audit_path, current_identity)
```

Collect `current_identity` from the loaded deployed runtime, as the normal host
provider does on each dispatch. This step validates the actual current source, external identities, audit,
manifest, patch, test logs, and every original native observation again. It
writes only the existing `application_pool_transport_proof` setting. Normal
`resolve(store_or_db, current_identity)` revalidates these references after
restart. Missing, public, altered, or stale evidence yields no capability.
Keep all evidence in durable owner-only storage. The audit file, original proof,
receipts/results, manifest, patch, test report, and logs are runtime dependencies.

The returned host marker retains `version=3` and the original `evidence_reference`.
It adds `native_identity_digest`, `compatibility_reference={path,sha256}`, and
`verification_basis=native_proof_with_reviewed_runtime_compatibility`.
`deployment_identity_digest` describes the effective current deployment. These
fields do not authorize a worker to install or change its checkpoint marker.
Existing host assignment/lease code remains responsible for marker propagation.

## Exact review artifact formats

The runtime manifest is a JSON array. Include only changed reviewed runtime
files, not tests or documentation. Before and after hashes must differ:

```json
[{"file":"readiness.py","before_sha256":"<64 hex>","after_sha256":"<64 hex>"}]
```

Generate the patch from files sorted by name. Concatenate Python
`difflib.unified_diff(old.read_text().splitlines(keepends=True),
new.read_text().splitlines(keepends=True), fromfile="a/"+name,
tofile="b/"+name)` without another separator. Encode it as UTF-8. Preparation
compares its exact bytes to the actual installed/candidate source difference.

The test report is JSON with exactly these fields:

```json
{
  "version":1,
  "status":"passed",
  "source_manifest_sha256":"<raw manifest file SHA256>",
  "suites":[{
    "command":"the actual offline test command",
    "run":100,"passed":98,"skipped":2,
    "log":{"path":"/durable/private/test.log","sha256":"<raw log SHA256>"}
  }]
}
```

Every suite must have positive passed tests and `run = passed + skipped`.
The report records operator-reviewed results; the compatibility code does not
parse test output or claim a test result independently of that review. All
reference files must be regular, absolute, owner-only files, bounded to 2 MiB.
The full deployment manifest can include tests and docs separately.

## Focused checks

`test_application_pool_compatibility.py` uses fictional private files only. It
checks baseline-before-change, exact candidate/patch/test matching, preserved
native evidence, changed-source invalidation, native/config identity mismatch,
audit/log tampering, immutable audit creation, no implicit capability, and
restart after the candidate directory is deleted. Existing proof, transport,
lease, checkpoint, gate, and session safety tests remain required.
