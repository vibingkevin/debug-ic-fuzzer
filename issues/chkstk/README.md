# `___chkstk_darwin` provider selection

## Symptom

On Xcode 27, `ld-prime` can select different providers for `___chkstk_darwin` when repeatedly linking identical iOS arm64 inputs. It either imports the symbol from the system or includes the static compiler-rt implementation, changing the binary’s size, layout, and SHA-256.

## Cause

Both the SDK’s `libSystem.B.tbd` and `libclang_rt.ios.a` provide `___chkstk_darwin` for the tested iOS 15 deployment target. With `-lSystem` before compiler-rt, identical link commands have selected either provider. The minimal reproducer isolates this to the final linker; the exact internal `ld-prime` race is not proven. Reported to Apple as FB25525111.

## Reproduce

On an Apple Silicon Mac with Xcode 27 selected, run from the repository root:

```sh
bash issues/chkstk/reproduce.sh
```

No Kotlin, Gradle, or Python is required. The script assembles a single branch to `___chkstk_darwin` and links the unchanged object 100 times per library order. On the triggering toolchain, `system-first` produces multiple SHA-256 values and `compiler-rt-first` produces one. It exits with an error if either expectation is not met.

## Stabilize

Order libraries as **compiler-rt → `-lSystem` → user static libraries**. This consistently selected the static compiler-rt implementation in the tested setup and keeps stack probing enabled. `-reproducible` alone did not stabilize the output. Disabling stack probing does not remove references already present in third-party objects. Revalidate after changing the toolchain or target, since library ordering can affect other symbols.

## Affected projects

- Animeko
