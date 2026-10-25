# Export verification

The exported iOS Swift package passed all 62 tests. The 20 question draft/sync
checks are a subset of that total. The generic iOS Simulator build succeeded with
`CODE_SIGNING_ALLOWED=NO` and no configured development team.

These checks do not install or launch an app on a simulator or physical iPhone.
Local device identities, signing settings, screenshots, and deployment history
remain outside this source repository.

See [the export verification](../VERIFICATION.md) for commands and limits.
