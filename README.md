# debug-ic-fuzzer

Replay a commit range with baseline and sut, using the same absolute project path and separate worktrees and project/dependency caches; compiler system caches may be shared.
Build and compare Linux products or iOS frameworks, and collect Native cache snapshots. No functional tests, cross-commit analysis, or automatic clean.

Requires Python 3.12+, Git, and a JDK. Linux x64 requires `llvm-strip` on PATH (for example, install the `llvm` package on Ubuntu).
iOS frameworks require an Apple Silicon Mac with Xcode selected by `xcode-select`; comparison uses `xcrun strip`, `codesign`, and the installed `macholib` dependency.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/debug-ic-fuzzer --compilers configs/compilers.yaml --project configs/project.yaml
```

For iOS use `--project configs/ic-sample-ios.yaml` and a compiler configuration with macOS distributions, or omit both Native homes to let KGP download them.
The manually triggered `Animeko` GitHub Actions workflow replays `configs/animeko.yaml` with `configs/compilers.yaml` on macOS ARM.

Compiler and project configurations are separate YAML files in `configs/`.
Relative CLI configuration paths resolve from `PROJECT_ROOT`, independent of the shell working directory.
`start_commit` and `end_commit` must lie on the selected branch's main line, with start at or before end.
Both endpoints are included. `forward` replays oldest to newest; `reverse` replays the same range newest to oldest.
Order follows commit ancestry, not timestamps; commits within merged side branches are not replayed individually.
Equal endpoints build one commit. Invalid endpoints or direction stop the run before compilation.
A Native home directory must be absolute. A `.tgz` or `.tar.gz` path may be relative to the compiler YAML;
archives are extracted into fresh `/tmp` directories for each run and removed when the run ends.
Without a home, the tool overrides KGP and lets it download the matching Native distribution into the tool-local `.konan` directory.
The tool does not pass `kotlin.native.version`. Both variants may use the same Native home and system cache.
`gradle_args` are passed through, replacing `{config_dir}` with the project YAML directory.

Each run:

1. Clone or update `repos/<repository-name>.git`. Different URLs with the same repository name use separate remotes; matching URLs reuse the existing remote.
2. Create `run/<compiler-config>-<project-config>-<timestamp>/` and two independent worktrees at the first commit in replay order.
   The timestamp uses `YYYYMMDD-HHMMSS`; an existing directory is an error.
3. Move baseline to `project`, check out the commit, run Gradle, parse the log, validate, collect caches and product hashes, and write its JSON.
   Only then move it back; failures also restore the worktree.
4. Repeat for sut, then compare the collected records before advancing to the next commit.
   Retain both worktrees' build outputs and caches.
   Write logs directly to files and read them only after the build finishes.

Each commit writes two JSON records and two Gradle logs directly in the run directory, named
`0000-<commit12>-baseline.json`, `0000-<commit12>-baseline.gradle.log`, and the corresponding sut files.
The zero-based index follows replay order. Gradle uses its default log level; KGP reports compiler arguments at warning level.
Logs include konanc arguments, without normalization or deduplication.
A failed build or result check still saves the record and allows the other variant of that commit to run, then stops replay.
Records contain the command, compiler path, duration, exit code, final link details, `cache_files`, and `artifacts`.
Each commit also writes `0000-<commit12>-comparison.json` with `strict_equal` and `production_equal`.
If both links are UP-TO-DATE, both equality fields are null with a `skipped` reason; replay continues.
If only one link is UP-TO-DATE, save the executed side's products, record an execution-state error, and stop.
Production differences or collection errors stop replay; differences only in the unstripped product do not.
Missing files are differences. Compiler caches are collected, not compared during replay.

After replay finishes or stops on a failed pair, `impl/summary.py` writes `summary.csv` from the JSON records and Git history.
Columns: `commit`, `changed_files`, `added_lines`, `deleted_lines`, `strict_equal`, `production_equal`,
`baseline_seconds`, `sut_seconds`, `baseline_rebuilt_caches`, `sut_rebuilt_caches`.
Changes are relative to the commit's first parent (empty tree for the initial commit), including in reverse replay.
Binary files count as changed files but contribute no lines; renames count as deletion plus addition.
Rebuilt counts are unique archive paths in the compiler report, recorded as `rebuilt_cache_count` before collection.
UP-TO-DATE counts as zero rebuilds; unknown counts and unperformed comparisons are blank. Durations are seconds.

Collection and comparison:

- `cache_files`: collect only cache units listed in the current `-Xdump-built-caches-to` report.
  Each reported `<cache-unit>/bin/*.a` selects its containing cache-unit directory, including metadata and IR; duplicate directories are scanned once.
  For per-file caches this selects only the rebuilt file's unit, not its unchanged siblings; monolithic caches select the whole library unit.
  An empty report records `[]`; UP-TO-DATE records null. Missing reports after failed builds do not trigger a fallback scan.
  Record `path`, `md5`, and `mtime_ns`. Known paths use `project/` or `native-home/` prefixes; other reported paths remain absolute.
- Cache `metadata.properties` and `bitcode_deps` are saved after converting line endings to LF and stripping surrounding whitespace.
  Remove `compilerFingerprint` from metadata before hashing and saving. Cache `.a`, IR, and other files are hashed as bytes but not saved.
- `artifacts`: inspect the final product directory (`.kexe`, `.so` plus its generated `.h`, or a flat iOS `.framework` with its sibling `.dSYM`).
  Each file records `path`, raw `md5`, `production_md5`, and `mtime_ns`.
  The production view strips ELF symbols/debug information and removes `.note.gnu.build-id`; headers use normalized text.
  Other output files use their original bytes. The original files are never modified.
- Dynamic framework binaries use a temporary copy stripped of debug/local symbols, code signatures, and UUID values.
  Static frameworks strip debug information and normalize archive timestamps, preserving symbols and relocations needed for linking.
  Framework headers, modulemaps, Info.plist, and resources participate in production comparison.
  dSYM files have null `production_md5` and participate only in strict comparison, including when present on only one side.
  This supports individual iOS frameworks, not XCFramework containers or versioned macOS framework symlinks.
- Compare file sets and raw MD5s for strict equality, and production MD5s for production equality.
  Save both variants' original product contents only on differences or errors. Only this save step translates paths back to baseline/sut.
  Equal products retain both hashes without saving contents. Temporary production views are always deleted.
- Saved contents are deduplicated immediately in `store/<filename>/<filename>-<md5>` shared by the run.
  Records do not repeat the derived store path. Cache archive contents are not retained for historical inspection; replay to reproduce them.

Strip errors are recorded and stop replay. A production difference is a comparison failure, not proof of a compiler bug.
Sources and outputs remain in their worktrees. `repos/` and `run/` are ignored by Git.

The tool sets `GRADLE_USER_HOME=<tool-root>/.gradle-home` and passes the matching `--gradle-user-home`, so both the Wrapper and Gradle use the shared directory instead of the inherited home.
All projects, runs, and both compiler variants share this directory and reuse downloaded dependencies.
Gradle receives `KONAN_DATA_DIR=<tool-root>/.konan`, overriding the inherited environment without changing the parent process.
The final link uses `-Xtemporary-files-dir=<run>/project/.tmp`, cleared before and after successful execution.
Gradle also receives `ZERO_AR_DATE=1` to remove object timestamps from Mach-O debug maps and archive headers.
The init script sets `-Xauto-cache-dir` to `<run>/project/.auto-cache`.
Each worktree retains its own dependency cache; moving it to `project` gives both variants the same build path. The project IC path comes from `-Xic-cache-dir`, including module-local build directories.
The compiler's `klib/cache` remains shared when Native homes match; existing system caches are not cleared.
The tool root is `PROJECT_ROOT` in `impl/config.py`, resolved from that Python file; `.gradle-home/` and `.konan/` are ignored by Git.
Build Cache and Configuration Cache follow project settings; only the final Native link is excluded from Build Cache reads and writes.
The capture action supports Configuration Cache and does not disable UP-TO-DATE checks.
UP-TO-DATE links retain the command, task state, duration, and log, with null `link`, `cache_files`, and `artifacts`.
They do not collect existing outputs or reuse metadata from previous commits.
Execution-state mismatch is a test failure requiring investigation, not proof of a compiler bug.

`debug_ic_fuzzer.py` coordinates both variants. `impl/config.py` handles configuration and distributions;
`impl/repository.py` prepares repositories; `impl/build.py` runs Gradle and parses the completed log once.
`impl/validation.py` checks link results.
`impl/collection.py` reads the current built-cache report and snapshots its reported cache directories.
`impl/comparison.py` collects product hashes and compares records.
Both collectors run inside `project`; each build JSON is written once before the worktree moves back.
`build_project` groups project directory/config, compiler config/home, and the log stem.
The replay loop expands config placeholders and adds commit/variant fields; run paths come from the project directory's parent.
`impl/elf.py` and `impl/macho.py` each expose `is_binary` and `compute_production_md5`.
`impl/comparison.py` dispatches by binary format; Mach-O handling also covers static framework archives.
`capture.init.gradle` overrides KGP, excludes the final link from Build Cache, reports the actual compiler path, and requests the built-cache archive list.

### Inlining options

The demo uses default inlining behavior; scenario commits do not change inlining options.
Kotlin/Native 2.4.21 supports these values for `-Xklib-ir-inliner=`:

| Value | Behavior while producing KLIBs |
|---|---|
| `default` | Choose the default mode for the language version |
| `intra-module` | Inline functions from the current module early |
| `full` | Also inline functions from dependencies early; 2.4.21 marks the KLIB as pre-release |
| `disabled` | Disable early inlining at this stage; later Native inlining can still occur |

Kotlin 2.4 defaults to intra-module early inlining; 2.5 extends this to cross-module inlining.
See the [official release notes](https://kotlinlang.org/docs/whatsnew-eap.html#consistent-cross-module-function-inlining-during-klib-compilation).
These are compiler options, not Gradle arguments. This tool does not set inlining options.

Linux tests also require `cc` and `objcopy` for small ELF fixtures. macOS tests build dynamic/static framework fixtures with Xcode tools; tests for the other platform are skipped.

```sh
.venv/bin/python -m pytest -q
```

### Formatting

With a Python 3.12+ environment active, the same commands work on Linux, macOS, and Windows:

```sh
python -m pip install -e ".[test,format]"
python -m ruff format .
```

Use `python -m ruff format --check .` to check without changing files.
The pinned Ruff version reads `pyproject.toml`: 240-column lines and no forced wrapping from trailing commas.
Formatting needs no editor extension.
