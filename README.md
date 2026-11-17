# debug-ic-fuzzer
 [![ShiguangSchedule](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/shiguangschedule.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/shiguangschedule.yml?query=branch%3Amain)
[![Animeko](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/animeko.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/animeko.yml?query=branch%3Amain) [![Confetti](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/confetti.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/confetti.yml?query=branch%3Amain) [![FeedFlow](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/feed-flow.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/feed-flow.yml?query=branch%3Amain) [![IC sample](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/ic-sample.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/ic-sample.yml?query=branch%3Amain) [![Keyguard](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/keyguard.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/keyguard.yml?query=branch%3Amain) [![KotlinConf](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/kotlinconf.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/kotlinconf.yml?query=branch%3Amain) [![Movies](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/movies.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/movies.yml?query=branch%3Amain) [![Music Assistant](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/music-assistant.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/music-assistant.yml?query=branch%3Amain) [![ShowcaseApp](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/showcase.yml/badge.svg?branch=main)](https://github.com/vibingkevin/debug-ic-fuzzer/actions/workflows/showcase.yml?query=branch%3Amain)

Find the commit where two Kotlin/Native incremental builds start producing different output. The tool replays a Git history with a `baseline` compiler and a `sut` (system under test), compares their products after each commit, and records essential info for investigation.

By default, both compilers keep their own build state as the replay advances. Using the same compiler on both sides checks debug build repeatability of that compiler; using a different `sut` compares a candidate compiler against the baseline. Either side can instead perform Native IC links after clearing project-generated cache files. It builds native products and does not run any tests.

## Run the IC sample

Use Linux x64 or macOS (Apple Silicon or Intel) with Python 3.12+, Git, and a JDK compatible with the sample (Github Action uses JDK 21). Linux needs `llvm-strip` on `PATH`; macOS needs Xcode selected with `xcode-select`. Run these commands from this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/debug-ic-fuzzer \
  --compilers configs/compilers-2.4.21.yaml \
  --project configs/ic-sample-linux.yaml
# On macOS, use --project configs/ic-sample-ios.yaml instead
```

The compiler configuration above uses Kotlin 2.4.21 on both sides. Gradle downloads the Native distribution on first use. To use the checked-in distributions with the inline DWARF fix, install Git LFS, run `git lfs pull`, and select `configs/compilers-2.4.21-9267.yaml`. That configuration also uses the same compiler on both sides; it selects the archive for the current host.

You can find the logs for the run under `run` folder.

## Read the result

Start with `summary.csv` in the printed `run/<compiler-config>-<project-config>-<timestamp>/` directory. Each row represents one commit and contains the comparison result, each Gradle invocation’s duration, and the number of cache archives reported as rebuilt.

There are two equality checks. `strict_equal` compares original product bytes, including debug information. `production_equal` compares a normalized view that removes debug data and binary identity differences while retaining code, headers, and resources. Production equality is a byte-comparison result, not a functional correctness test.

| Result | What happens next |
| --- | --- |
| Both checks are true | Continue to the next commit. |
| Strict differs, production matches | Save the original products and continue; inspect the debug or identity difference separately. |
| Production differs | Save the original products and stop after this commit pair. |
| Both builds return nonzero exit codes | Leave both equality checks blank and record `Both Failed` in the CSV `note` column, then continue. Exit codes and failure reasons need not match; products are not compared. Some commits just won't compile for native iOS target. |
| Both links are `UP-TO-DATE` | Skip comparison and continue. Equality fields are blank in CSV and `null` in JSON; the CSV `note` column records `Both UP-TO-DATE`. |
| Only one build fails, only one link is `UP-TO-DATE`, or collection fails after a successful build | Record the error and stop after this commit pair. |

For a failing or differing row, open its `0000-<commit12>-comparison.json` first, then the corresponding `-baseline.json`, `-sut.json`, and `.gradle.log` files. JSON records contain the commands, compiler paths, errors, artifact hashes, and cache snapshots. Saved product contents are under that run’s `store/` directory.

A replay whose commit pairs are all accepted returns exit code 0. A failed comparison or one-sided build failure returns 1. An exception that aborts preparation or replay returns 2 and bypasses summary generation. See [Replay details](docs/replay.md) for comparison rules, cache scope, and evidence retention.

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

To replay part of the commits from the same YAML, add a zero-based offset and a maximum count:

```sh
.venv/bin/debug-ic-fuzzer --compilers configs/compilers-2.4.21-9267.yaml --project configs/confetti.yaml --start-at 50 --commit-count 50
```

Paging applies after selecting the YAML range and its `direction`. With no paging options, the full range is replayed. Omit `--commit-count` to replay everything from `--start-at`; the last page may be shorter than the requested count. Negative offsets, nonpositive counts, and offsets selecting no commits return exit code 2. Each run writes its actual ordered commit SHAs and page bounds to `range.json`; result filenames use page-local indices.

Use `gradle_args` for project properties and extra init scripts. `{config_dir}` expands to the project YAML’s directory, so an init-script argument can be written as `{config_dir}/project.init.gradle`.

The [FeedFlow configuration](configs/feed-flow.yaml) starts at the first Kotlin 2.4 commit and overrides older SKIE releases with 0.10.16 for Kotlin 2.4.21 compatibility. It requires a Swift toolchain with `-prefix-map-sourceinfo`, which zeros source-info timestamps, and disables Clang module breadcrumbs in static Swift objects. Apple Swift 6.3.1 and 6.3.2 do not support the source-info flag; the official [2026-11-01 Swift development snapshot](https://www.swift.org/install/macos/) does. The comparison rules remain unchanged.

Install the snapshot as a dedicated writable toolchain alongside Xcode. Kotlin/Native finds SKIE's Swift compiler relative to the linker returned by `xcrun --find ld`, so selecting Swift alone is insufficient. Copy Xcode's linker into the dedicated toolchain (a symlink resolves back to Xcode), then verify both selected paths before replaying:

```sh
swift_toolchain_bin="$(dirname "$(xcrun --toolchain org.swift.65202611011a --find swiftc)")"
cp -n "$(xcrun --toolchain XcodeDefault --find ld)" "$swift_toolchain_bin/ld"
TOOLCHAINS=org.swift.65202611011a xcrun --find ld
TOOLCHAINS=org.swift.65202611011a xcrun swiftc --version
TOOLCHAINS=org.swift.65202611011a .venv/bin/debug-ic-fuzzer --compilers configs/compilers-2.4.21.yaml --project configs/feed-flow.yaml
```

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

### Compare against a clean Native build

Set `clean_build: true` independently on `baseline`, `sut`, or both in the compiler YAML. It defaults to `false`. For example, compare clean baseline output with incremental SUT output using the same compiler:

```yaml
compilers:
  baseline:
    kotlin_version: "2.4.21"
    clean_build: true
  sut:
    kotlin_version: "2.4.21"
```

Clean mode currently supports KGP 2.4.x. It keeps Native IC enabled, clears the project `.auto-cache` and `kotlin-native-ic-cache` directories before each final link, and executes that link for every commit. Kotlin/Native distribution caches for stdlib and platform libraries remain available. It preserves KLIB compilation, dependency downloads, and unrelated build state; it does not invoke Gradle `clean`. When either side is clean, both final links execute even for commits that leave Native inputs unchanged, so each commit has products to compare. The incremental side retains its project Native cache state. JSON records include `clean_build`; cache snapshots record what was rebuilt from the empty project cache.

The IC sample workflow runs both incremental-baseline/incremental-SUT and clean-baseline/incremental-SUT comparisons on Linux x64, macOS arm64, and macOS x64. Both modes use the existing byte-comparison verdict and upload separate results. Dependency caches can be restored across modes, but each job uses a distinct save key and no Native IC state is cached.

Clean and cached compilation can produce different normalized binary bytes even with the same compiler and matching application behavior. The existing byte-comparison rules still apply, including stopping on a production difference. A difference between these modes is evidence to investigate, not proof of a functional compiler bug.

Clean mode deliberately does not use `kotlin.native.cacheKind=none` or the `disableNativeCache` DSL: those disable Native IC itself. Instead, the init script removes only project-generated cache directories immediately before the link and records that condition in the `IC_FUZZER` report. The compiler still receives `-Xenable-incremental-compilation`; distribution stdlib and platformlib caches are not removed. This behavior requires revalidation when upgrading KGP beyond 2.4.

## Investigate a difference

[Replay details](docs/replay.md) explains which state is shared, how product bytes are normalized, and what the cache records establish. Isolated reproductions of known differences are documented separately:

- [Objective-C stub GOT ordering](issues/smallstub/): `ld-prime` layout variation and the small-stub workaround.
- [`___chkstk_darwin` provider selection](issues/chkstk/): `ld-prime` chooses between system and compiler-rt implementations.
- [Inline DWARF source ownership](issues/inline-dwarf/): Kotlin/Native source-location errors, fixed on upstream `master`.

## Rejected Projects

[SimpMusic](https://github.com/maxrave-dev/SimpMusic): the upstream [project guide](https://github.com/maxrave-dev/SimpMusic/blob/f78a4fd6da9680e5771f4763194d2895d26914fd/CLAUDE.md) lists iOS as future work.

## Development

```sh
.venv/bin/python -m pip install -e '.[test,format]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff format --check .
```

Linux ELF tests need `cc`, `objcopy`, and `llvm-strip`; on Ubuntu these are provided by `build-essential`, `binutils`, and `llvm`. macOS framework tests use Xcode’s `clang` and `libtool`. Platform-specific tests are skipped on other hosts.
