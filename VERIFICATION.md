# Export verification — October 16, 2026

This is a source-only export. No Linux deployment or data migration has run.

- The 42 source/contract/fixture files match SOURCE_MANIFEST.json.
- Python suite: 247 tests discovered; 225 passed and 22 optional Caddy integration tests skipped because JOB_REVIEW_CADDY was not supplied. Runtime: 33.504 seconds.
- Legacy Node UI checks passed.
- Export used an explicit file allowlist. No production database, state directory, real catalog, user research, credential, browser profile, or platform binary is included.
- Candidate sources were checked against the local device bearer credentials without displaying them; no match was found. Private-key and GitHub-token markers were also checked.
- Tests used fictional temporary databases. The running Mac backend, iOS app, production decisions, browser session, and existing workspace Git state were not changed.

Linux must run its own tests and verify systemd, HTTPS, desktop access, browser persistence, and any production data cutover.
