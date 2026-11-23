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

Production comparison now zeroes only serialized `.swiftsourceinfo` mtime fields, while strict comparison retains the original file hash. Path, interface hash, size, and declaration differences remain visible.

`-Xfrontend -prefix-map-sourceinfo` remains a possible compiler-side optimization once the project's supported Swift toolchain provides it. The option zeros serialized source mtimes and applies configured path mappings; see [Swift PR #90671](https://github.com/swiftlang/swift/pull/90671). The comparator normalization is already active and does not require changing the compiler or incremental-build inputs.

As checked on 2026-11-19, the source of [Swift 6.4.x development snapshot 2026-11-09](https://github.com/swiftlang/swift/blob/swift-6.4.x-DEVELOPMENT-SNAPSHOT-2026-11-09-a/include/swift/Option/FrontendOptions.td) and `release/6.5.x` contains the option. Swift 6.4.2 and `release/6.4.3` do not, and a local Swift 6.4 compiler rejects it. The [macos-latest image](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md) defaults to Xcode 26.6; the separate [xcode-27 image](https://github.com/actions/runner-images/blob/main/images/macos/xcode-27-arm64-Readme.md) lists Xcode 27.2, which [contains Swift 6.4.2](https://forums.swift.org/t/announcing-swift-6-4-2/90337). Merely selecting a newer runner does not provide this option.

The comparator uses the normalization path until a supported Swift toolchain is available. It does not change source mtimes before compilation, which would alter incremental-build inputs.

Timestamp-only changes are normalized while meaningful source-info changes still differ. The affected project replay remains the CI regression check.
