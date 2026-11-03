# Kotlin 2.4.21: IC rebuild scope changes inline source locations

Verified on ma1 with upstream Animeko `01175358bd6bceae2cccd4d011939190f1a7070e`, community Kotlin 2.4.21, the iOS device framework task, and the existing explicit linker workaround.

The debug difference reported during the 30-commit replay is reproducible without interrupting a build. With unchanged KLIB inputs, rebuilding the complete project IC and rebuilding only eight cache files produce the same original framework binary but different dSYM files. The differing source locations already exist in six Kotlin-generated cache archives before Apple linking or dSYM generation.

## Controlled verification

The experiment retained Gradle dependencies, Native downloads, automatic dependency caches, and project build outputs. It temporarily moved the project IC directory aside, forced only the final Native task to execute, and then:

1. Rebuilt the project IC at the fixed `project` path: 1,709 rebuilt cache units.
2. Moved aside the eight cache-file directories reported by the original retry, then linked again: eight rebuilt units. All KLIB compile tasks were UP-TO-DATE.
3. Repeated both steps independently, then restored the original IC directory and source revision. A final build matched every original artifact hash from the end of the 30-commit replay.

| Comparison | Result |
| --- | --- |
| Complete rebuild, first versus second experiment | All artifact and recorded cache hashes match |
| Eight-file rebuild, first versus second experiment | All artifact and recorded cache hashes match |
| Complete versus eight-file rebuild | Original framework binary, headers, and production artifacts match |
| Complete versus eight-file rebuild | Six of eight raw cache archives differ |
| Those eight archives after `strip -S` | All match |
| Final differing files | dSYM DWARF binary and relocation YAML |

The eight files' IR fingerprints and recorded dependencies, inline bodies, class fields, eager initialization, and trivial getters match between rebuild scopes. This is deterministic sensitivity to the set of files rebuilt in one Native invocation. The original interrupted build exposed it; interruption is not a necessary trigger.

## Concrete debug differences

Inside the cache for `AniAppContent.kt`, the same inline records contain:

| DWARF location | Complete rebuild | Eight-file rebuild |
| --- | --- | --- |
| Inlined lambda declaration | `AniApp.kt:142` | `AniAppContent.kt:115` |
| Another lambda declaration | `BangumiConflictNotifier.kt:111` | `AniAppContent.kt:600` |
| That inline call's column | 40855 | 17249 |

`DW_AT_decl_file`, `DW_AT_decl_line`, `DW_AT_call_file`, `DW_AT_call_line`, and `DW_AT_call_column` change. The first affected final compilation unit starts at DWARF offset `0x864a1a`. All 239,739 relocation records match after omitting their debug offsets; symbol names, object addresses, final addresses, and symbol sizes match.

## Inliner source path

A further cache-child IR dump pins the first divergence to `InlineAllFunctions`. `AniAppContent`'s complete IR dump is identical before the phase. Afterward, exactly six `inlinedFunctionFileEntry` paths differ, matching the later DWARF differences. These dumps were captured with diagnostic flags in `additionalCacheFlags.ios_arm64`, so the actual cache compilations received them.

The corresponding source path in Kotlin 2.4.21 is `FunctionInlining.visitFunctionAccess()`: it recursively transforms `actualCallee.body` in place and then its default arguments before `CallInlining.inlineFunction()` copies the callee. The recursive transformation retains the caller's `fileEntriesStack`. `InlinePostprocessor.visitCall()` writes that current file entry into lambda `IrInlinedFunctionBlock` nodes. Later visits reuse already-expanded blocks and their stored file entries. Consequently, shared inline templates can retain source locations from an earlier consuming file; changing the files compiled together changes the retained location.

`IrToBitcode.InlinedBlockScope` then emits these lambda scopes from `inlinedFunctionFileEntry` and interprets source offsets using that file entry, explaining the different filenames, lines, and columns. The phase and field divergence are measured; the source path explains their propagation. A proposed source correction must preserve the callee's file context during recursive template expansion, but no compiler patch has been validated yet.

## Evidence

Under ma1's existing `run/compilers-animeko-five-repro-20261103-032007/dwarf-investigation/`:

- `whole-build.json`, `partial-build.json`, and `repeat/`: successful build records and hashes.
- `whole/` and `partial/`: the eight original archives and decoded DWARF.
- `main-cache-dwarf.diff`: the source-file, line, and column changes before Apple linking.
- `archive-comparison.json`: raw versus debug-stripped archive comparison.
- `cache-ir-probe/BEFORE.diff` and `AFTER.diff`: identical input IR and the six inline-file-entry changes.

The replay's original `summary.csv` and failure records remain unchanged. Keep reporting the strict difference; production equality does not establish debug-info correctness.
