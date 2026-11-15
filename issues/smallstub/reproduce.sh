#!/usr/bin/env bash
# Reproduce Apple Objective-C stub GOT ordering without Kotlin.
set -euo pipefail

calls=${OBJC_CALLS:-2048}
repeats=${OBJC_REPEATS:-20}
clang=$(xcrun --find clang)
ld=$(xcrun --find ld)
sdk=$(xcrun --sdk iphoneos --show-sdk-path)
sdk_version=$(xcrun --sdk iphoneos --show-sdk-version)

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
cd "$work_dir"

{
    printf '.text\n.globl _entry\n_entry:\n'
    for ((i = 0; i < calls; i++)); do
        printf '  bl _objc_msgSend$sel%d\n  bl _objc_msgSend\n' "$i"
    done
    printf '  ret\n'
} > calls.s

"$clang" -target arm64-apple-ios15.0 -isysroot "$sdk" -c calls.s -o calls.o

repeat_link() {
    local label=$1
    shift
    for ((i = 0; i < repeats; i++)); do
        ZERO_AR_DATE=1 "$ld" -dylib -arch arm64 -platform_version ios 15.0 "$sdk_version" \
            -syslibroot "$sdk" calls.o -o result.dylib -lobjc -lSystem "$@"
        shasum -a 256 result.dylib >> "$label.hashes"
    done
    echo "$label (count, SHA-256, file):"
    sort "$label.hashes" | uniq -c
}

# Only the stub mode changes between the two runs.
repeat_link fast
repeat_link small -objc_stubs_small

if [ "$(sort -u fast.hashes | wc -l)" -lt 2 ]; then
    echo "fast mode did not vary; try a different Apple linker or increase OBJC_CALLS" >&2
    exit 1
fi
if [ "$(sort -u small.hashes | wc -l)" -ne 1 ]; then
    echo "small-stub mode was not stable" >&2
    exit 1
fi
