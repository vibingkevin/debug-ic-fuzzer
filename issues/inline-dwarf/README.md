# Inline DWARF source ownership

## Symptom

Kotlin/Native 2.4.21 without the fix below can emit incorrect source locations for nested inline lambdas. With the same KLIB inputs, a Debug link can produce different inline DWARF depending on which Native cache files are rebuilt together. The final framework binary can remain byte-identical while its dSYM and cache-object DWARF differ.

## Cause

When recursively expanding a shared inline callee, the IR inliner uses the caller’s file context instead of the callee’s definition file. Lambda source information stored in the expanded body can therefore inherit the first caller’s file. Later callers reuse that body, so changing the cache rebuild scope can change the recorded source location.

The inliner also needs to distinguish caller-supplied lambda arguments from default lambdas defined in the callee. Losing that distinction assigns the wrong file to an inline lambda. Kotlin/Native then emits those locations into DWARF.

## Reproduce

Requirements: macOS with Xcode selected, a JDK, Python 3, and an affected Kotlin/Native 2.4.21 distribution. The script also needs `llvm-ar` and `llvm-dwarfdump` in the distribution’s `konan/bin` or `bin`, or on `PATH`. Run from the repository root:

```sh
# Set this to an unpatched Kotlin/Native distribution.
KONAN_HOME=/path/to/kotlin-native-prebuilt-macos-aarch64-2.4.21 \
  python3 issues/inline-dwarf/reproduce.py
```

The script creates an external inline function and two callers, builds the full cache twice as a stability control, then changes one caller. It compares a one-file incremental rebuild with a project-IC-clean rebuild of the same updated KLIBs, hashing the final binary and DWARF extracted from a matching cache object.

The expected reproduction is equal binary hashes but different DWARF hashes. The script exits with an error if that split is not observed; it is a reproducer for the old bug, not a pass/fail test for fixed compilers. A DWARF difference alone does not demonstrate different machine code or runtime behavior.

## Stabilize

Fixed on Kotlin `master` by [PR #9267](https://github.com/JetBrains/kotlin/pull/9267), addressing [KT-90639](https://youtrack.jetbrains.com/issue/KT-90639). The fix expands shared callee bodies in their definition’s file context and preserves the source file of each inline lambda argument, including default lambdas.

Use a Kotlin/Native compiler containing the fix and rebuild the Native caches. On an older compiler, keeping the same cache rebuild scope can make comparisons consistent, but does not correct the source locations. Keep strict dSYM comparisons enabled: binary equality alone does not establish correct debug information.

## Affected projects

- Animeko
