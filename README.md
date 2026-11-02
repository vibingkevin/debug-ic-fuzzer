# debug-ic-fuzzer

Replay a commit range with baseline and sut, using the same absolute project path and separate worktrees and Native caches.
Build and compare Linux products, and collect Native cache snapshots. No functional tests, cross-commit analysis, or automatic clean.

Requires Linux x64, Python 3.12+, Git, a JDK, and `llvm-strip`. iOS framework comparison is not implemented.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/debug-ic-fuzzer --compilers configs/compilers.yaml --project configs/project.yaml
```

Compiler and project configurations are separate YAML files in `configs/`.
`start_commit` and `end_commit` must lie on the selected branch's main line, with start at or before end.
Both endpoints are included. `forward` replays oldest to newest; `reverse` replays the same range newest to oldest.
Order follows commit ancestry, not timestamps; commits within merged side branches are not replayed individually.
Equal endpoints build one commit. Invalid endpoints or direction stop the run before compilation.
A Native home directory must be absolute. A `.tgz` or `.tar.gz` path may be relative to the compiler YAML;
archives are extracted into fresh `/tmp` directories for each run and removed when the run ends.
Without a home, the tool overrides KGP and lets it download the matching Native distribution into `$KONAN_DATA_DIR` (default: `~/.konan`).
The tool does not pass `kotlin.native.version`. Both variants resolving to the same Native cache directory is an error.
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
Production differences or collection errors stop replay; differences only in the unstripped product do not.
Missing files are differences. Compiler caches are collected, not compared during replay.

Collection and comparison:

- `cache_files`: scan the single `-Xic-cache-dir` and the compiler's `klib/cache`, after every build including UP-TO-DATE.
  Record `path`, `md5`, and `mtime_ns`. Paths start with `project/` or `native-home/`, never baseline/sut.
- Cache `metadata.properties` and `bitcode_deps` are saved after converting line endings to LF and stripping surrounding whitespace.
  Remove `compilerFingerprint` from metadata before hashing and saving. Cache `.a`, IR, and other files are hashed as bytes but not saved.
- `artifacts`: inspect the final Linux product directory (`.kexe`, or `.so` plus its generated `.h`).
  Each file records `path`, raw `md5`, `production_md5`, and `mtime_ns`.
  The production view strips ELF symbols/debug information and removes `.note.gnu.build-id`; headers use normalized text.
  Other output files use their original bytes. The original files are never modified.
- Compare file sets and raw MD5s for strict equality, and production MD5s for production equality.
  Save both variants' original product contents only on differences or errors. Only this save step translates paths back to baseline/sut.
  Equal products retain both hashes without saving contents. Temporary production views are always deleted.
- Saved contents are deduplicated immediately in `store/<filename>/<filename>-<md5>` shared by the run.
  Records do not repeat the derived store path. Cache archive contents are not retained for historical inspection; replay to reproduce them.
- The compiler's built-cache report checks actual archive paths against the two tracked cache roots.
  External cache overrides, escaping paths, shared compiler cache roots, and cache/product symlinks are rejected.

Strip errors are recorded and stop replay. A production difference is a comparison failure, not proof of a compiler bug.
Sources and outputs remain in their worktrees. `repos/` and `run/` are ignored by Git.

The tool passes `--gradle-user-home <tool-root>/.gradle-home`, overriding the inherited `GRADLE_USER_HOME`.
All projects, runs, and both compiler variants share this directory and reuse downloaded dependencies.
The tool root is the working directory from which you launch the runner; `.gradle-home/` is ignored by Git.
Build Cache and Configuration Cache follow project settings; only the final Native link is excluded from Build Cache reads and writes.
The capture action supports Configuration Cache and does not disable UP-TO-DATE checks.
The initial product build in each fresh worktree must execute the link task.
Later UP-TO-DATE links reuse the previously reported task metadata and check that the output still exists.
Records include the link task state. There is no comparison with previous results.

`debug_ic_fuzzer.py` coordinates both variants. `impl/config.py` handles configuration and distributions;
`impl/repository.py` prepares repositories; `impl/build.py` runs Gradle, parses the completed log once,
and resolves cache paths, including reused UP-TO-DATE metadata.
`impl/validation.py` checks link results, cache boundaries, and the current invocation's built-cache report.
`impl/collection.py` only accepts directories and a store, then snapshots files; it does not read build logs or task states.
`impl/comparison.py` collects product hashes and compares records.
Both collectors run inside `project`; each build JSON is written once before the worktree moves back.
Platform-specific production normalization stays in `production_digest`; future iOS support can reuse the record format.
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

Tests also require `cc` and `objcopy` for small ELF fixtures.

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
Formatting needs no editor extension; the fuzzer itself still targets Linux.
