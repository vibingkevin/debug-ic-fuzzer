# Understanding a replay

Use this guide after the [first run](../README.md#run-the-ic-sample), when a difference needs explanation. The comparison is between baseline and sut at the same commit. It does not compare a commit with its parent, or compare an incremental build with a separate clean build.

## How build state carries forward

Each run starts two new worktrees at the first selected commit. For every commit, the tool builds baseline, builds sut, then compares their recorded products. Each worktree retains its outputs and project cache state for the next commit.

Both builds use the same absolute source path, `run/<run-name>/project/`. The tool moves a variant’s worktree into that location for the build and moves it back to `baseline/` or `sut/` afterward. This avoids embedding different baseline/sut source paths in the products while preserving separate build histories.

That separation does not make every cache independent:

| State | Scope |
| --- | --- |
| Worktree outputs, project IC caches, `.auto-cache/` | Retained separately by each variant within the run. |
| `.gradle-home/` and `.konan/` under the tool root | Shared across variants and runs, including dependency downloads. |
| Stdlib and platformlib caches in `<Native home>/klib/cache` | Shared when both variants resolve to the same Native home; existing caches are retained. |
| Native homes supplied as archives | Extracted separately for each variant, even when both select the same archive. |

The init script disables Build Cache reads and writes for the selected final Native link. It preserves `UP-TO-DATE` checks, Configuration Cache, and other tasks’ Build Cache behavior. It redirects automatic library caches to the active worktree’s `.auto-cache/`; KGP manages the project IC cache. The active worktree’s `.tmp/` is cleared before an executed link, and builds use `ZERO_AR_DATE=1`.

This is why a faster build does not by itself prove a compiler improvement. The CSV duration covers the entire Gradle invocation, and shared downloads or distribution caches can affect it. Use the recorded command, task state, and Gradle log to explain a timing difference.

## What product equality means

The collector accepts ELF products such as `.kexe` and `.so`, and flat iOS `.framework` products containing a Mach-O binary or static archive. Shared libraries require a generated header; frameworks require headers, `Info.plist`, and a module map. Product symlinks are rejected.

Collection includes every file under the final output’s parent directory, including a sibling `.dSYM`. The two checks use the same logical paths:

- **Strict:** compare file presence and raw MD5 hashes.
- **Production:** compare file presence and MD5 hashes after the transformations below. dSYM files are excluded from this view.

| File type | Production-view transformation |
| --- | --- |
| ELF | Strip symbols and debug information; remove `.note.gnu.build-id`. |
| Dynamic Mach-O | Strip debug and local symbols, remove code signatures, and zero UUIDs. |
| Static framework archive | Strip debug information and normalize archive timestamps; retain symbols and relocations needed for linking. |
| Header | Normalize line endings to LF and strip surrounding whitespace. |
| Module map, plist, resources, other files | Hash unchanged. |

Normalization operates on temporary copies. The original products remain available for debugging. A production difference stops replay; a strict-only difference is retained for inspection and does not stop it. Neither result establishes application behavior, since the tool does not execute the products.

## Use cache records to explain the result

Cache snapshots show what the compiler reported rebuilding. They do not participate in the automatic equality verdict.

For an executed link, the tool reads `-Xdump-built-caches-to`. Each reported archive selects its containing cache unit. A per-file cache selects that file’s unit; a monolithic cache selects the whole library. The collector hashes the files in those units, including metadata and IR, without scanning unrelated cache directories.

`rebuilt_cache_count` counts unique archive paths in that report. It is not a count of all Kotlin files compiled. An empty report gives zero; `UP-TO-DATE` gives zero in the CSV without collecting cache or product files. Missing or unperformed measurements remain blank. A successful executed link requires its cache report; a failed build without one does not trigger a fallback cache scan.

`metadata.properties` and `bitcode_deps` are normalized before hashing and storage: line endings become LF, surrounding whitespace is stripped, and `compilerFingerprint` is removed from metadata. Other cache files retain raw hashes and are not copied into the evidence store.

## Follow the evidence from summary to files

Each completed commit pair has a shared prefix, such as `0000-<commit12>`. Start broad and narrow down:

1. **`summary.csv`** identifies the commit, equality results, whole-build durations, and rebuilt-cache counts. Change counts compare each commit with its first parent, regardless of replay direction; a root commit is compared with the empty tree.
2. **`<prefix>-comparison.json`** gives the equality flags, differing paths, or the error/skip reason.
3. **`<prefix>-baseline.json` and `-sut.json`** give commands, compiler homes, task states, errors, artifact hashes, and cache-file records. Their `.gradle.log` siblings contain the complete Gradle output.
4. **`store/` inside the run directory** holds normalized cache metadata and saved original products, deduplicated as `<filename>/<filename>-<MD5>`.

Strict product differences save both sides’ collected original products. Error paths save products already present in the artifact records; a build that failed before collection has no recorded product to save. Equal products retain hashes only. Worktrees are retained, but subsequent commits overwrite build outputs, so use saved evidence for earlier commits.

When both links are `UP-TO-DATE`, the comparison records a skip with `null` equality values. If just one side is `UP-TO-DATE`, the comparison records an execution-state error. Recorded build or collection failures still allow the other variant to finish that commit before replay stops. Exceptions that escape the replay loop abort immediately and bypass CSV generation; logs and JSON already written remain on disk.

The [execution diagram](execution-flow.html) provides a visual map of the original implementation and pins its source links to the revision recorded in the diagram. For current behavior, the entry point is [debug_ic_fuzzer.py](../debug_ic_fuzzer.py), with [build setup](../capture.init.gradle), [product comparison](../impl/comparison.py), and [cache collection](../impl/collection.py) split into separate modules.
