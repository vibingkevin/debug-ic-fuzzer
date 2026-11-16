# debug-ic-fuzzer

Find the commit where two Kotlin/Native incremental builds start producing different output. The tool replays a Git history with a `baseline` compiler and a `sut` (system under test), compares their products after each commit, and records the builds for investigation.

Both sides keep their own build state as the replay advances. Using the same compiler on both sides checks repeatability; changing `sut` compares a candidate compiler against the baseline. This is an incremental replay, not a clean rebuild of every commit. It builds Native products and does not run application tests.

## Run the IC sample

Use Linux x64 or macOS (Apple Silicon or Intel) with Python 3.12+, Git, and a JDK compatible with the sample (the CI uses JDK 21). Linux needs `llvm-strip` on `PATH`; macOS needs Xcode selected with `xcode-select`. Run these commands from this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/debug-ic-fuzzer \
  --compilers configs/compilers-2.4.21.yaml \
  --project configs/ic-sample-linux.yaml
```

On Apple Silicon macOS, use `configs/ic-sample-ios.yaml` instead. The Linux sample builds an executable; the macOS sample builds an iOS simulator framework. Both replay from the configured baseline through the latest commit on `main`.

On Intel macOS, select a project task targeting `iosArm64` for iOS IC replay. The official Kotlin/Native 2.4.21 distribution lists `macos_x64`, `ios_x64`, and `ios_arm64` in `cacheableTargets.macos_x64`; it does not list `ios_simulator_arm64`. An Intel-hosted simulator ARM64 link can succeed without enabling IC or writing a built-cache report. Confirm `-Xenable-incremental-compilation` and `-Xic-cache-dir` in the link arguments; passing `kotlin.incremental.native=true` alone does not establish that IC ran.

The compiler configuration above uses Kotlin 2.4.21 on both sides. Gradle downloads the Native distribution on first use. To use the checked-in distributions with the inline DWARF fix, install Git LFS, run `git lfs pull`, and select `configs/compilers-2.4.21-9267.yaml`. That configuration also uses the same compiler on both sides; it selects the archive for the current host.

After repository preparation, a progress bar advances once per completed commit pair. Gradle output goes into log files rather than streaming to the terminal. The tool prints the run directory when replay finishes.

## Read the result

Start with `summary.csv` in the printed `run/<compiler-config>-<project-config>-<timestamp>/` directory. Each row represents one commit and contains the comparison result, each Gradle invocation’s duration, and the number of cache archives reported as rebuilt.

There are two equality checks. `strict_equal` compares original product bytes, including debug information. `production_equal` compares a normalized view that removes debug data and binary identity differences while retaining code, headers, and resources. Production equality is a byte-comparison result, not a functional correctness test.

| Result | What happens next |
| --- | --- |
| Both checks are true | Continue to the next commit. |
| Strict differs, production matches | Save the original products and continue; inspect the debug or identity difference separately. |
| Production differs | Save the original products and stop after this commit pair. |
| Both builds return nonzero exit codes | Record both equality checks as true and `both_failed=true`, then continue. Exit codes and failure reasons need not match; products are not compared. |
| Both links are `UP-TO-DATE` | Skip comparison and continue. Equality fields are blank in CSV and `null` in JSON. |
| Only one build fails, only one link is `UP-TO-DATE`, or collection fails after a successful build | Record the error and stop after this commit pair. |

For a failing or differing row, open its `0000-<commit12>-comparison.json` first, then the corresponding `-baseline.json`, `-sut.json`, and `.gradle.log` files. JSON records contain the commands, compiler paths, errors, artifact hashes, and cache snapshots. Saved product contents are under that run’s `store/` directory.

A replay whose commit pairs are all accepted returns exit code 0, including pairs where both builds fail. A failed comparison or one-sided build failure returns 1. An exception that aborts preparation or replay returns 2 and bypasses summary generation. See [Replay details](docs/replay.md) for comparison rules, cache scope, and evidence retention.

## Replay your project

Choose one final Native link task and a commit range that builds with the selected compilers. The repository must provide `gradlew`. Save a project YAML such as:

```yaml
repository: "https://github.com/example/project.git"
branch: "main"
start_commit: "<full commit ID>"
end_commit: "latest"
direction: forward
task: ":app:linkDebugExecutableLinuxX64"
gradle_args: []
```

Both endpoints are included. `latest` resolves to the fetched tip of `branch`. The range follows first-parent history; `reverse` visits the same range backwards, and equal endpoints build just one commit. An endpoint missing from that history is an error. When the upstream history is rewritten, update the configured commit IDs.

To replay a page from the same YAML, add a zero-based offset and a maximum count:

```sh
.venv/bin/debug-ic-fuzzer --compilers configs/compilers-2.4.21-9267.yaml --project configs/confetti.yaml --start-at 50 --commit-count 50
```

Paging applies after selecting the YAML range and its `direction`. With no paging options, the full range is replayed. Omit `--commit-count` to replay everything from `--start-at`; the last page may be shorter than the requested count. Negative offsets, nonpositive counts, and offsets selecting no commits return exit code 2. Each run writes its actual ordered commit SHAs and page bounds to `range.json`; result filenames use page-local indices.

Each page starts with independent baseline/sut build state at its first selected commit. It retains incremental state within the page and does not replay the preceding commits, so separate pages do not cover incremental transitions across page boundaries. For append-only upstream history, new commits extend the last page until it fills; a later run needs another page after that. History rewrites can change page membership.

The manually dispatched [Confetti workflow](.github/workflows/confetti.yml) runs offsets 0 through 350 in groups of 50 using the same YAML. An extra guard page at offset 400 must report an empty range. If it finds commits, the workflow fails to request another normal page and a new guard offset. Only the specific empty-range error is accepted by the guard; build and setup failures still fail CI. Every job resolves `latest` independently and uploads its resolved `range.json`.

Use `gradle_args` for project properties and extra init scripts. `{config_dir}` expands to the project YAML’s directory, so an init-script argument can be written as `{config_dir}/project.init.gradle`.

The supplied [Animeko configuration](configs/animeko-openani.yaml) demonstrates project-specific setup and Apple linker workarounds. The [Workbench configuration](configs/workbench-ohos.yaml) targets OHOS and lists its SDK, toolchain, and host requirements. These projects need their own build prerequisites in addition to the fuzzer’s dependencies.

## Select the compilers

A compiler YAML requires exactly `baseline` and `sut`. `kotlin_version` selects the Kotlin Gradle plugin version; the tool verifies the loaded plugin and the Native home used by the link. To test a local Native compiler while keeping KGP fixed:

```yaml
compilers:
  baseline:
    kotlin_version: "2.4.21"
  sut:
    kotlin_version: "2.4.21"
    kotlin_native_home: "/absolute/path/to/kotlin-native"
```

Without `kotlin_native_home`, KGP manages the distribution under the tool’s `.konan/` directory. With it, the tool passes the resolved directory as `kotlin.native.home`. An archive is extracted into a fresh `/tmp` directory for each variant and removed when the run exits.

| Input | Path or selection rule |
| --- | --- |
| Relative `--compilers` or `--project` path | Resolved from the directory containing `debug_ic_fuzzer.py` (`PROJECT_ROOT`). |
| Native distribution directory | Requires an absolute path containing `bin/konanc`. |
| Relative `.tgz` or `.tar.gz` path | Resolved from the compiler YAML’s directory. |
| List of archives | Exactly one filename must contain the current host token: `linux-x86_64`, `macos-aarch64`, or `macos-x86_64`. |

Absolute CLI and archive paths are used directly. The tool leaves the project’s inlining options unchanged.

## Investigate a difference

[Replay details](docs/replay.md) explains which state is shared, how product bytes are normalized, and what the cache records establish. Isolated reproductions of known differences are documented separately:

- [Objective-C stub GOT ordering](issues/smallstub/): `ld-prime` layout variation and the small-stub workaround.
- [`___chkstk_darwin` provider selection](issues/chkstk/): `ld-prime` chooses between system and compiler-rt implementations.
- [Inline DWARF source ownership](issues/inline-dwarf/): Kotlin/Native source-location errors, fixed on upstream `master`.

## Projects not included

[SimpMusic](https://github.com/maxrave-dev/SimpMusic): the upstream [project guide](https://github.com/maxrave-dev/SimpMusic/blob/f78a4fd6da9680e5771f4763194d2895d26914fd/CLAUDE.md) lists iOS as future work. Our [iOS sublibrary build attempts failed](https://github.com/vibingkevin/debug-ic-fuzzer-validation/actions/runs/43736378961/job/132879879034), so the project is not included in iOS replay tests.

## Development

To monitor the latest run of each active workflow on `main`, authenticate the GitHub CLI and run `python3 scripts/watch_actions.py`. It checks every minute, reports failed matrix jobs as soon as they finish, and saves a status snapshot and failure logs under `.actions-monitor/`. It exits after all latest runs finish (0 for success, 1 for failures, 2 for an initial API error); use `--once` for one check or `--watch` to keep following new runs. The script only observes runs; fixes and reruns require inspecting the saved evidence.

```sh
.venv/bin/python -m pip install -e '.[test,format]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff format --check .
```

Linux ELF tests need `cc`, `objcopy`, and `llvm-strip`; on Ubuntu these are provided by `build-essential`, `binutils`, and `llvm`. macOS framework tests use Xcode’s `clang` and `libtool`. Platform-specific tests are skipped on other hosts.
