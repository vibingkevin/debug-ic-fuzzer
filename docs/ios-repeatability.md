# Animeko iOS device framework repeatability

Verified on ma1 on 2026-11-03: upstream Animeko `741b66951a0f739576880dd9ba99b6daa0d113bd`, community KGP/Kotlin Native 2.4.21, Xcode 27.0 linker 27037.1. The task is `:app:shared:application:linkPodDebugFrameworkIosArm64`; the binary's build command reports platform `IOS`, not `IOSSIMULATOR`. This builds a device framework without running an app on a phone.

Fixed project/temporary/cache paths and `ZERO_AR_DATE=1` remove the previously observed path and object-timestamp differences. Two independent Apple linker choices remain:

- `___chkstk_darwin` sometimes becomes an import from libSystem and sometimes comes from `libclang_rt.ios.a`, changing text size and layout. A five-line assembly file containing only a branch to this symbol reproduces two outputs over 100 links with Kotlin's library order. Moving System/runtime after the user libraries gives one output over 100 links.
- Ordinary calls and generated Objective-C selector stubs use two GOT slots for the same `_objc_msgSend`. Their slot assignment varies, changing instruction encodings. `-objc_stubs_small` avoids that second slot by routing selector stubs through the ordinary message-send stub.

Both failures reproduce by invoking the Apple linker directly on unchanged inputs. SHA-256 checks of 2,161 physical inputs, including the object, selected cache archives, third-party frameworks, and linker, match before and after the experiments. Logical SDK dependencies without physical files are recorded separately. This isolates these failures from Kotlin compilation and IC dirty propagation; it does not identify the exact internal race or algorithm in Apple's shipped linker.

| Full Animeko link configuration | Repetitions | Distinct raw binaries |
| --- | ---: | ---: |
| Original | 12 | 4 |
| Add `-reproducible` | 10 | 4 |
| Small stubs only | 20 | 2 |
| System/runtime after user libraries | 15 | 2 |
| That order plus small stubs | 30 | 1 |
| Compiler-rt before System plus small stubs | 30 | 1 |

## Use the verified workaround

`configs/animeko.yaml` enables the external script through its existing `gradle_args`:

```yaml
  - "-I"
  - "{config_dir}/animeko-reproducible.init.gradle"
```

The supplied init script keeps all Kotlin 2.4.21 `ios_arm64` platform linker flags except the early `-lSystem`, then adds `-lSystem` after compiler-rt and selects small stubs. Both baseline and sut receive the same options. The compiler distribution, IC settings, and comparison rules are unchanged. Remove these two Gradle arguments to reproduce the original linker behavior.

Five real Gradle/Kotlin links with these options produced identical complete artifact inventories, including the original framework binary, headers, metadata, and dSYM. Every link rebuilt zero Native cache units. Framework MD5: `a838ec7a49a75652cfd5b91be45b356e`; production MD5: `e4248cb7e84112736dbafe19915d45e4`. This verifies repeated warm links at one commit.

A subsequent fuzzer replay built five consecutive commits with separate baseline/sut worktrees, both using community 2.4.21 and this init script. All 10 Native links executed; all five pairs passed both strict and production comparison, including dSYM. Rebuilt cache counts matched on both sides:

| Commit | Rebuilt cache units per variant |
| --- | ---: |
| `741b66951a0f` | 1823 |
| `366484bce7bb` | 8 |
| `aa75e3834b35` | 33 |
| `853f1f697c54` | 405 |
| `f9843a046e72` | 439 |

The first pair started with empty project/dependency caches and shared existing compiler system caches. This is not a fully empty compiler-cache clean-rebuild test. The compiler, app, and target scope above still applies.

The same run was extended to 30 consecutive commits, ending at `79a4fc8b2d0005b91a0b51b8ee98a0f5125d4e89`, without relocating or clearing either worktree. It reused the tool's Gradle home, downloaded Native distribution, project IC, and build outputs. Of the 30 pairs, 22 executed both Native links and passed production comparison; 21 also passed strict comparison. Eight pairs were UP-TO-DATE on both sides and were not compared. The endpoint changes Swift application code, so the last actual framework comparison was at `616eaae77a2faa2d482d26574f15316607770a0e`.

The one strict difference was at `01175358bd6bceae2cccd4d011939190f1a7070e`. AboutLibraries stalled downloading license text. During diagnosis baseline completed; stopping the owned Gradle daemon interrupted sut after it had reached Native linking. Baseline's successful record was retained and sut was retried with a temporary license-domain proxy, without rerouting Maven traffic or clearing build caches. The original framework binary and all production artifacts matched, but the dSYM DWARF binary and relocation YAML differed. The original failed-attempt log and differing products were retained. This pair is not an uninterrupted replay sample: its timings and rebuilt-cache counts (507 versus 8 on the successful retry) must not be compared as performance or optimization results. The relocation records differ only in debug offsets; their symbols, object addresses, and final binary addresses match. A subsequent uninterrupted experiment reproduced this difference by rebuilding all project IC and then only the eight files. It is Kotlin inline source-location sensitivity to rebuild scope; see [the controlled verification](native-inline-debug-info.md). Subsequent actual links at `ac46b01980da` and `616eaae77a2f` passed strict comparison again.

The 30-pair records, `summary.csv`, `audit.json`, and retry details are under ma1's existing `run/compilers-animeko-five-repro-20261103-032007` directory. Its original name and stable project path were kept intentionally. No comparison rules or compiler binaries were changed for this extension.

The override is specific to the tested Kotlin version's platform flags. Recheck it when upgrading. Small stubs add an extra branch in Objective-C dispatch; Apple describes the size/performance tradeoff in [WWDC22](https://developer.apple.com/videos/play/wwdc2022/110363/). Runtime performance was not measured. This is an explicit test workaround, not a compiler or Apple linker fix.

For an upstream fix, adjust the already-deterministic runtime-library order in Kotlin's `MacOSBasedLinker.finalLinkCommands()` and report the duplicate message-send GOT ordering to Apple. Removing UUID/debug data or teaching the comparator to ignore differing instructions would conceal the failure.

See [the reproducer and handoff](../reproducers/apple-linker/HANDOFF.md) for commands, evidence locations, and unresolved questions.
