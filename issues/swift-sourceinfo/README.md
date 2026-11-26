# Swift source-info timestamps

NYTimes-KMP produces different `.swiftsourceinfo` files for baseline and SUT even when both builds use the same active project directory. Swift records each generated source file's last-modified time, so generating the same SKIE sources at different times changes the output.

## Evidence

[GHA run 44496328665](https://github.com/vibingkevin/debug-ic-fuzzer/actions/runs/44496328665/job/135465000844), first commit `0c73205838161a4f12e1c0aefe44900d59f63d37`, Kotlin 2.4.21, Apple Swift 6.3.3, iOS arm64:

- Both source-info files are 327,804 bytes and contain 108 source-file records.
- Paths, interface hashes, source sizes, declaration locations, USRs, and documentation ranges match.
- All 108 source-file mtimes differ, accounting for 539 differing bytes.
- Zeroing only these mtime fields makes the complete files byte-identical.
- Baseline MD5: `a9551b4c5c40471df77a3633031ba134`; SUT: `cbf501252b60c33a22c94ddf3d9df86b`.

These are source mtimes inside `SOURCE_FILE_LIST`, not the filesystem mtime of the output. Swift writes them as nanoseconds since the epoch in [SerializeDoc.cpp](https://github.com/swiftlang/swift/blob/swift-6.3.3-RELEASE/lib/Serialization/SerializeDoc.cpp#L721).

## Current workaround and follow-up

The replay now uses Swift's compiler-side `-Xfrontend -prefix-map-sourceinfo` option. Production comparison hashes `.swiftsourceinfo` normally, while strict comparison retains the original file hash.

`-Xfrontend -prefix-map-sourceinfo` zeros serialized source mtimes and applies configured path mappings; see [Swift PR #90671](https://github.com/swiftlang/swift/pull/90671). FeedFlow passes it through SKIE's Swift compiler arguments and pins the Swift 6.5 development snapshot that provides the option.

As checked on 2026-11-24, the source of [Swift 6.5 development snapshot 2026-11-01](https://github.com/swiftlang/swift/blob/swift-DEVELOPMENT-SNAPSHOT-2026-11-01-a/include/swift/Option/FrontendOptions.td) contains the option. The Xcode toolchains on the current macOS runners reject it, so the workflow verifies the installed snapshot path before replaying.

The affected project replay remains the CI regression check. If a future toolchain removes the option, the workflow's explicit frontend-help check fails before replaying commits.
