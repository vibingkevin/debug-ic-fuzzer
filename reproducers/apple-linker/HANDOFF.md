# Apple device linker reproducibility handoff

## Scope and current implementation

Host: ma1, Apple Silicon, Xcode 27.0, Apple linker 27037.1. Target: `iosArm64` / Mach-O `IOS`, minimum iOS 15.0. Community Kotlin/KGP 2.4.21; upstream Animeko `741b66951a0f739576880dd9ba99b6daa0d113bd`. No phone execution was performed.

`configs/animeko.yaml` loads `configs/animeko-reproducible.init.gradle` through ordinary Gradle arguments:

```yaml
gradle_args:
  # Keep the project's other arguments here.
  - "-I"
  - "{config_dir}/animeko-reproducible.init.gradle"
```

The fuzzer only expands `{config_dir}` and forwards the arguments. Gradle loads the script, which sets `toolOptions.freeCompilerArgs` on `:app:shared:application:linkPodDebugFrameworkIosArm64`:

1. Override `linkerKonanFlags.ios_arm64`, preserving its Kotlin 2.4.21 flags except the early `-lSystem`.
2. Add `-linker-option -lSystem`. Kotlin places this later, after compiler-rt.
3. Add `-linker-option -objc_stubs_small`, making selector stubs reuse the ordinary message-send stub/GOT entry.

These are Native compiler options passed by an init script, not options Gradle itself understands. No new fuzzer property or compiler-binary patch is involved. Remove the two entries in the `-I`/script pair to disable this workaround. The script is specific to this task and the tested version's platform flags.

General fuzzer isolation remains in the tool: both worktrees compile at `project`, temporary files use `project/.tmp`, dependency caches use `project/.auto-cache`, and the process receives `ZERO_AR_DATE=1`. These are separate from the external linker workaround. Comparison rules still check complete original artifacts.

## Small standalone reproduction: runtime symbol selection

Requirements: Python 3.9+, an Apple Silicon Mac, and Xcode. No Kotlin, Gradle, CocoaPods, or Animeko checkout is needed. From the fuzzer repository root:

```sh
export DEVELOPER_DIR=/Applications/Xcode-27.0.0.app/Contents/Developer
python3 reproducers/apple-linker/reproduce.py run/apple-linker-repro-1
```

Use a new output directory for each invocation. The script assembles `stack-probe.s` once, then runs 100 links per library order with the same object, output path, and SDK. The assembly only branches to `___chkstk_darwin`. The SDK framework list is retained from the triggering case and has not been fully minimized.

Expected failure signature: `system_first` produces multiple raw MD5 values. Compare the saved `*.symbols.txt`: one output imports `___chkstk_darwin` from libSystem; another contains compiler-rt definitions including `___chkstk_darwin_probe`. The `runtime_first` control should produce one output in the verified environment. `results.json` records exact commands and input hashes; each distinct binary is saved once. Compiler/link errors fail the command; an exit code of zero means the experiment finished, not that nondeterminism was necessarily reproduced.

Packaging validation on ma1: the original order produced two outputs (74/26) in 100 links; runtime-first produced one output in 100 links.

Changing Xcode, SDK, or the library list can change whether it triggers. A single observed output does not prove absence of the bug. Platform metadata `ios 15.0 26.4` deliberately matches the failing Kotlin invocation; the actual SDK is located using `xcrun`.

## ObjC GOT reproduction: retained full link, not yet minimal

No small standalone ObjC reproduction has been established. Small three-reference assembly examples were stable. Do not present the runtime-only reproduction above as reproducing this second problem.

The ma1 tool checkout is under `kotlin-dsh/workspace/01a18d53-0194-72f0-8f32-7cb9b93a2f12/debug-ic-fuzzer`. Relative to that checkout, the frozen replay directory is:

```text
run/animeko-iosArm64-2.4.21-20261103-020357/device-link-handoff/
```

It contains a copied main object and filelist, `ld-command.json`, `runtime-first-command.json`, explicit-input hashes, distinct binary samples, and `results.json`. Native archives and third-party frameworks remain referenced from the original run's `project` directory. Keep that directory: this replay is host-local, not a self-contained export. `runtime-first-command.json` stabilizes the runtime provider so the remaining variation isolates the two objc_msgSend GOT slots.

From that ma1 checkout, replay the frozen command without Gradle or Kotlin:

```sh
python3 - <<'PY'
from collections import Counter
from pathlib import Path
import hashlib, json, os, subprocess
root = Path('run/animeko-iosArm64-2.4.21-20261103-020357/device-link-handoff')
command = json.loads((root / 'runtime-first-command.json').read_text())
output = Path(command[command.index('-o') + 1])
for mode, extra in [('fast', []), ('small', ['-objc_stubs_small'])]:
    hashes = Counter()
    for _ in range(20):
        subprocess.run(command + extra, env={**os.environ, 'ZERO_AR_DATE': '1'}, check=True, capture_output=True)
        content = output.read_bytes()
        digest = hashlib.md5(content).hexdigest()
        hashes[digest] += 1
        sample = root / (digest + '.dylib')
        if not sample.exists():
            sample.write_bytes(content)
    print(mode, dict(hashes))
PY
```

Packaging validation of this frozen replay: fast stubs produced two outputs (11/9) in 20 links; small stubs produced one output in 20 links. Explicit input hashes remained unchanged.

Inspect distinct samples using `xcrun dyld_info -fixups`, `xcrun nm -m`, and `xcrun llvm-objdump --disassemble`. In the failing case two GOT slots bind the same `libobjc/_objc_msgSend`; ordinary references and selector stubs swap which slot they use. With small stubs the verified product has one such slot.

## Established evidence and limits

- The original full link produced four raw outputs in 12 attempts. `-reproducible` did not fix it. Small stubs alone left runtime-provider variation; ordering alone left GOT variation.
- During fixed-input experiments, 2,161 physical input SHA-256 values were unchanged. Logical SDK dependency names without physical files are listed separately.
- Five real Gradle/Kotlin links with both workarounds had identical complete framework/dSYM inventories.
- A five-commit replay (`741b66951a0f` through `f9843a046e72`) executed all 10 baseline/sut links with community 2.4.21. Every pair passed strict and production equality. Rebuilt units per variant: 1823, 8, 33, 405, 439.
- Evidence on ma1: `device-link-diagnosis/` and `workaround-*.json` in the original run; full replay JSON/logs/CSV in `run/compilers-animeko-five-repro-20261103-032007/`. Local copies are under `run/ma1-iosArm64-linker-fix/` in the fixed tool checkout.
- Compiler binaries, IC settings, and comparison logic were not patched. Small stubs add a jump; runtime performance and phone execution are untested. Compiler system caches were shared, so the five-commit replay is not a fully empty compiler-cache clean test.

## Next owner

1. Minimize the ObjC case while preserving both GOT references and selector stubs; use the frozen full replay as the oracle.
2. Locate the exact failure in Apple's shipped linker. Fixed command order does not imply fixed internal symbol resolution. Parallel processing, hash iteration, and equal-name ordering are hypotheses, not established causes.
3. For Kotlin, inspect `native/utils/src/org/jetbrains/kotlin/konan/target/Linker.kt`, `MacOSBasedLinker.finalLinkCommands()`. Its argument order is already deterministic. Evaluate changing runtime placement across targets/deployment versions rather than claiming Kotlin randomly orders libraries.
4. The public [ld64 ObjC pass](https://github.com/apple-oss-distributions/ld64/blob/main/src/ld/passes/objc_stubs.cpp) documents the fast/small mechanism, but is not proof of the internal algorithm in linker 27037.1. Do not attribute the observed failure to its name comparator without matching implementation evidence.
5. Keep strict comparison intact. Do not normalize away differing instructions or treat a workaround as an upstream linker fix. No upstream issue or patch has been submitted by this handoff.
