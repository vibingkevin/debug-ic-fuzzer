# debug-ic-fuzzer

debug-ic-fuzzer replays an inclusive commit range with a baseline compiler and a system-under-test compiler. It builds both variants from the same absolute project path, gives them independent worktrees and project/dependency caches, and compares the resulting Native products and selected cache units. The stdlib and platformlib caches are shared when both variants use the same Native home. The tool is intended for build repeatability and incremental-cache investigations, it does not run functional tests.

## Prerequisites

Supported hosts: Apple Silicon macOS and Linux x64.

Runtime dependencies:

- Python 3.12 or newer, Git, and a JDK compatible with the project being built.
- Linux: `llvm-strip` for ELF production-byte comparisons (provided by the `llvm` package on Ubuntu).
- macOS: Xcode selected with `xcode-select`, providing the tools used for Mach-O stripping and signature removal.

Additional test dependencies:

- Linux: a C compiler available as `cc` and `objcopy`, used to create and modify ELF test fixtures (provided by `build-essential` and `binutils` on Ubuntu).
- macOS: the selected Xcode’s `clang` and `libtool`, used to create framework test fixtures.

## Installation and first run

Create a virtual environment, install the project, and invoke the console entry point with one compiler YAML and one project YAML:

~~~sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/debug-ic-fuzzer \
  --compilers configs/compilers-2.4.21.yaml \
  --project configs/ic-sample-ios.yaml
~~~

For iOS, use a compiler configuration containing macOS Native distributions and an iOS project configuration such as configs/ic-sample-ios.yaml. If a compiler YAML omits kotlin_native_home, KGP downloads the matching Native distribution into the tool-local .konan directory. The manually triggered Animeko workflow uses configs/openani-animeko.yaml on a macOS ARM runner.

## Configuration

Compiler and project settings live in separate YAML files. The following single example shows the accepted fields and their meaning; the File: comments mark where the two parts belong and are explanatory comments, not extra schema:

~~~yaml
# File: configs/compilers-2.4.21.yaml
compilers:
  # Both entries are required. The fuzzer builds each commit with both variants.
  baseline:
    kotlin_version: "2.4.21"       # Required compiler version passed to KGP.
    # Optional absolute Native home or a path relative to this compiler YAML.
    # A .tgz/.tar.gz archive is extracted into a fresh /tmp directory per run.
    kotlin_native_home: "../compilers/kotlin-native-prebuilt-linux-x86_64-2.4.21.tar.gz"
  sut:
    kotlin_version: "2.4.21"
    kotlin_native_home: "../compilers/kotlin-native-prebuilt-linux-x86_64-2.4.21.tar.gz"

# File: configs/project.yaml
repository: "https://github.com/example/project.git" # HTTPS, SSH, or git@ URL.
branch: "main"                         # Must contain both commit endpoints.
start_commit: "<oldest-commit>"        # Inclusive; use a full commit ID.
end_commit: "<newest-commit>"          # Inclusive; latest resolves the fetched branch tip.
direction: forward                     # forward or reverse replay order.
task: ":app:linkDebugExecutableLinuxX64" # Final Native link task to execute.
gradle_args:                             # Optional arguments forwarded to Gradle.
  - "-Pfeature=true"
~~~

Relative CLI paths are resolved from PROJECT_ROOT, not from the shell's current directory. A compiler home supplied as a directory must be absolute; an archive may be relative to the compiler YAML. A kotlin_native_home list is also accepted when it contains archives for multiple hosts, but exactly one filename must contain the current host token (linux-x86_64 or macos-aarch64). The project repository must be reachable by Git, task must be a Gradle task path, and both endpoints must be on the selected branch's main line. Equal endpoints build one commit. forward follows ancestry from oldest to newest, while reverse follows the same range in the opposite order; merge side branches are not replayed independently.

gradle_args are passed through unchanged after replacing {config_dir} with the directory containing the project YAML. The tool does not pass kotlin.native.version. When both variants resolve to the same Native home, they share its stdlib and platformlib caches.

## Replay and outputs

For each run the tool updates repos/<repository-name>.git, creates a timestamped directory under run/, and prepares two independent worktrees. It moves the baseline worktree to project, checks out the commit, runs Gradle, parses the completed log, validates the final link, and records the result before doing the same for the sut worktree. Both worktrees retain their build outputs and caches. The timestamp format is YYYYMMDD-HHMMSS, and an existing run directory is an error.

Each replayed commit produces two JSON records and two Gradle logs named 0000-<commit12>-baseline.json, 0000-<commit12>-baseline.gradle.log, and the corresponding sut files. The records include the command, compiler path, duration, exit code, final-link details, selected cache files, and artifact hashes. A third 0000-<commit12>-comparison.json records strict_equal and production_equal. If both links are UP-TO-DATE, the equality fields are null with a skipped reason and replay continues. If only one side is UP-TO-DATE, the tool records an execution-state error and stops. A production difference, missing file, strip failure, or collection error stops the replay; a difference only in the unstripped product does not.

After replay, impl/summary.py writes summary.csv from the records and Git history. It includes the commit, changed-file counts, line counts, equality results, durations, and rebuilt-cache counts. Rebuilt counts are unique archive paths reported by the compiler; UP-TO-DATE counts as zero, while unknown or unperformed values remain blank.

## Cache and artifact comparison

The collector reads only the cache units listed in the current -Xdump-built-caches-to report. A reported <cache-unit>/bin/*.a selects the containing unit, including metadata and IR; per-file caches select only the reported file unit, while monolithic caches select the whole library. An empty report becomes [], UP-TO-DATE becomes null, and a missing report after a failed build does not trigger a fallback scan. Cache metadata and dependency lists are normalized before hashing, with compilerFingerprint removed from metadata; binary cache contents are hashed but not retained.

The artifact collector supports .kexe, .so plus its generated header, and a flat iOS .framework with its sibling .dSYM. Strict comparison uses raw MD5 values for every file. Production comparison uses a temporary stripped view: ELF build IDs and debug information are removed, Mach-O symbols, signatures, and UUIDs are removed, and static archive timestamps are normalized. Headers, module maps, Info.plist, and resources remain part of the production view. dSYM files participate only in strict comparison. Differences and failed records retain the original product contents in the shared store/ directory; equal products retain hashes only.

The tool sets an isolated GRADLE_USER_HOME and KONAN_DATA_DIR under its root, uses ZERO_AR_DATE=1, gives each run a temporary-files directory, and sets project IC and automatic-cache directories through the init script. The stdlib and platformlib caches in `<Native home>/klib/cache` are shared when both variants resolve to the same home; -Xauto-cache-dir does not redirect these caches. Archive-based Native homes are extracted separately for each variant, so their stdlib and platformlib caches are independent. Existing stdlib and platformlib caches are not cleared. Build Cache and Configuration Cache follow the project settings, while the final Native link is excluded from Build Cache reads and writes. UP-TO-DATE links retain their command, task state, duration, and log but do not collect existing outputs.

## Inlining options

The default scenarios do not change Kotlin/Native inlining options. Kotlin/Native 2.4.21 accepts default, intra-module, full, and disabled for -Xklib-ir-inliner=. Kotlin 2.4 defaults to intra-module early inlining, while 2.5 extends the default to cross-module inlining. These are compiler options, not Gradle arguments, and this tool does not set them. See the official release notes for the language-version behavior: https://kotlinlang.org/docs/whatsnew-eap.html#consistent-cross-module-function-inlining-during-klib-compilation.

Known macOS linker and Kotlin/Native debug-information reproductions are kept under issues/:

- issues/smallstub/ covers Objective-C selector-stub GOT ordering.
- issues/chkstk/ covers ___chkstk_darwin provider selection.
- issues/inline-dwarf/ covers cache-scope-dependent inline DWARF file entries.

## Tests and formatting

With a Python 3.12+ environment active, run the test suite with .venv/bin/python -m pytest -q. Formatting dependencies can be installed with python -m pip install -e ".[test,format]"; use python -m ruff format . to format or python -m ruff format --check . to check without changing files. The pinned Ruff configuration allows 240-column lines and does not force-wrap trailing-comma expressions.
