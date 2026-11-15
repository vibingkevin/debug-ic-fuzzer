#!/usr/bin/env bash
# Reproduce Apple ___chkstk_darwin provider selection without Kotlin.
set -euo pipefail

clang=$(xcrun --find clang)
ld=$(xcrun --find ld)
sdk=$(xcrun --sdk iphoneos --show-sdk-path)
sdk_version=$(xcrun --sdk iphoneos --show-sdk-version)
compiler_rt="$("$clang" --print-resource-dir)/lib/darwin/libclang_rt.ios.a"

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
cd "$work_dir"

cat > probe.s <<'ASM'
.text
.globl _probe
_probe:
  b ___chkstk_darwin
ASM

"$clang" -target arm64-apple-ios15.0 -isysroot "$sdk" -c probe.s -o probe.o

repeat_link() {
    local label=$1
    shift
    for ((i = 0; i < 100; i++)); do
        "$ld" -dylib -arch arm64 -platform_version ios 15.0 "$sdk_version" \
            -syslibroot "$sdk" probe.o -o result.dylib "$@"
        shasum -a 256 result.dylib >> "$label.hashes"
    done
    echo "$label (count, SHA-256, file):"
    sort "$label.hashes" | uniq -c
}

# Only the library order changes between the two runs.
repeat_link system-first -lSystem "$compiler_rt"
repeat_link compiler-rt-first "$compiler_rt" -lSystem

if [ "$(sort -u system-first.hashes | wc -l)" -lt 2 ]; then
    echo "system-first did not vary on this linker" >&2
    exit 1
fi
if [ "$(sort -u compiler-rt-first.hashes | wc -l)" -ne 1 ]; then
    echo "compiler-rt-first was not stable" >&2
    exit 1
fi
