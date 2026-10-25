# Source export verification — October 25, 2026

Checks ran against this repository export. They do not install the apps, deploy a
backend, prove Linux browser support, or authorize an employer application.

| Check | Result |
| --- | --- |
| Python backend | 898 tests: 876 passed, 22 skipped |
| Legacy Node UI | Passed |
| Mac Swift package | 203 tests passed |
| iOS Swift package | 62 tests passed |
| Generic iOS Simulator build | Succeeded, signing disabled |
| Export scan | No private credential, origin, signing-team, or local-user-path matches |

The 22 Python skips require the real Caddy/TLS fixtures (`JOB_REVIEW_CADDY` and
OpenSSL, plus HTTP/2 support where applicable). The local deployment suite has
13 additional checks for one-off operator scripts; those scripts and their tests
are intentionally excluded from this portable export. Test suites overlap in
shared-code coverage; their counts are not a unique combined coverage total.

## Reproduce

From the repository root after installing `job-review/requirements-pool.txt`:

```sh
python3 -B -m unittest discover -s job-review -p 'test_*.py' -v
node job-review/test_ui.js
swift test --package-path job-review-mac --disable-sandbox
swift test --package-path job-review-ios --disable-sandbox
python3 job-review-ios/scripts/check_ios.py --build
```

Swift checks require Xcode on macOS. The simulator build uses
`CODE_SIGNING_ALLOWED=NO`; no physical device or signing account is required.
All test databases are temporary and fictional. Tests made no production API,
Chrome, native worker, employer, or device-installation calls.

## Export boundary

The export used an explicit source allowlist. It includes reusable backend
recovery/proof tools, both Swift clients, contracts, tests, fictional fixtures,
fonts with their license, and app assets. Runtime implementations match the
verified local source. Configuration examples use placeholders, the Xcode
signing team is blank, and documentation omits local chat/device history.

No state directory, database, saved answer pool, profile/resume, private native
receipt, browser profile, credential, deployment log, user screenshot, or one-off
production script is included. Candidate files were compared against the known
local device credential and origin without printing either. Private-key and
provider-token markers were also checked.

`SOURCE_MANIFEST.json` records the exact committed export file hashes. Browser
proof and compatibility evidence are host-specific private runtime inputs;
copying this repository does not grant browser dispatch capability.
