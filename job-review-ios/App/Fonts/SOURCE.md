# Inter font assets

Inter 4.1 by The Inter Project Authors. These static TrueType files are
unmodified files from the official release, distributed under the SIL Open
Font License 1.1 in `LICENSE.txt`.

- Release: https://github.com/rsms/inter/releases/tag/v4.1
- Archive: https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip
- Original paths: `extras/ttf/Inter-*.ttf`
- Retrieved: 2026-10-18

| File | PostScript name | SHA-256 |
| --- | --- | --- |
| Inter-Regular.ttf | Inter-Regular | 40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82 |
| Inter-Medium.ttf | Inter-Medium | 97ad806f526e41546d46365bb3a393145f75b7b1568913db74549ad8b8dba872 |
| Inter-SemiBold.ttf | Inter-SemiBold | 78a843fade9d4612a5567302fb595b56976eb5fcebf4fea5a5912d638bafcde3 |
| Inter-Bold.ttf | Inter-Bold | 288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f |

Xcode copies the `Fonts` folder into the app bundle. `UIAppFonts` in
`Info.plist` registers the four fonts on iOS. Swift Package Manager copies the
same folder into the `JobReviewPreview` resource bundle. The macOS preview
must register the font URLs from `Bundle.module`, using the `Fonts`
subdirectory, with Core Text for the current process.
