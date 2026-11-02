# debug-ic-fuzzer

Replay a commit range with baseline and sut, using the same absolute project path and separate worktrees and Native caches.
This version builds products only. It does not run functional tests, compare artifacts, count cache rebuilds, or automatically clean.

Requires Python 3.12+, Git, and a JDK. Supports Linux x64 and Apple Silicon Mac; iOS builds also require Xcode.

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
The tool does not pass `kotlin.native.version`. Both variants resolving to the same Native home is an error.
`gradle_args` are passed through, replacing `{config_dir}` with the project YAML directory.

Each run:

1. Clone or update the shared bare repository in `repos/`.
2. Create `run/<compiler-config>-<project-config>-<timestamp>/` and two independent worktrees at the first commit in replay order.
   The timestamp uses `YYYYMMDD-HHMMSS`; an existing directory is an error.
3. Move baseline to `project` with `git worktree move`, check out the requested commit, run the Gradle task, and move it back even on failure.
4. Repeat for sut, then advance both worktrees to the next commit, retaining their build outputs and caches.
   Write logs directly to files and read them only after the build finishes.

Each commit writes two JSON records and two Gradle logs directly in the run directory, named
`0000-<commit12>-baseline.json`, `0000-<commit12>-baseline.gradle.log`, and the corresponding sut files.
The zero-based index follows replay order. Logs include konanc arguments, without normalization or deduplication.
A failed build or result check still saves the record and allows the other variant of that commit to run, then stops replay.
Records contain the command, compiler path, duration, exit code, and final link details; artifacts and IC files are not scanned or archived.
Sources and outputs remain in their worktrees. `repos/` and `run/` are ignored by Git.

The tool inherits `GRADLE_USER_HOME`, defaulting to the global `~/.gradle`, and keeps downloaded dependencies.
Build Cache and Configuration Cache follow project settings; only the final Native link is excluded from Build Cache reads and writes.
The capture action supports Configuration Cache and does not disable UP-TO-DATE checks.
The initial product build in each fresh worktree must execute the link task.
Later UP-TO-DATE links reuse the previously reported task metadata and check that the output still exists.
Records include the link task state. There is no comparison with previous results.

`debug_ic_fuzzer.py` coordinates both variants. `impl/config.py` handles configuration and distributions;
`impl/repository.py` prepares repositories; `impl/build.py` runs and records a single build.
`capture.init.gradle` overrides KGP, excludes the final link from Build Cache, and reports the actual compiler path.

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

```sh
.venv/bin/python -m pytest -q
```
