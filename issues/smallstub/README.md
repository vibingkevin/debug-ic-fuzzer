# Objective-C stub GOT ordering

## Symptom

On Xcode 27, `ld-prime` can produce different Mach-O bytes when repeatedly linking identical iOS arm64 inputs containing both ordinary `_objc_msgSend` calls and selector stubs. In fast-stub mode, the relative order of two GOT entries for `_objc_msgSend` can change, altering the referencing instructions and binary layout.

## Cause

Fast mode keeps two direct paths:

```text
ordinary call  -> ordinary symbol stub -> GOT[ordinary] -> objc_msgSend
selector call  -> selector stub      -> GOT[objc]     -> objc_msgSend
```

The two GOT entries contain the same target, but they are separate linker atoms. This lets the selector stub skip the extra branch through the ordinary stub.

The two `_objc_msgSend` GOT atoms have the same visible name. Their relative order can depend on the linker’s internal atom construction order, while the name sort has no stable tie-breaker. Swapping the entries changes the page and offset encoded in the referencing instructions, so `__text`, stubs, UUID, and dSYM bytes change even though both entries still call the same runtime function. The exact scheduling point inside the shipped `ld-prime` is not established.

## Reproduce

On an Apple Silicon Mac with Xcode 27 selected, run from the repository root:

```sh
bash issues/smallstub/reproduce.sh
```

Kotlin / Gradle not required. The script generates one assembly object with 2,048 pairs of ordinary `_objc_msgSend` calls and selector calls such as `_objc_msgSend$sel0`. It links the unchanged object 20 times per mode, using the default fast stubs and then `-objc_stubs_small`.

On the triggering toolchain, `fast` produces multiple SHA-256 values and `small` produces one. The script exits with an error if either expectation is not met. Use `OBJC_CALLS` and `OBJC_REPEATS` to change the call-pair and repetition counts; if fast mode does not vary, try increasing `OBJC_CALLS` or using the triggering linker version.

## Stabilize

`-objc_stubs_small` reuses the ordinary message-send stub/GOT entry. The selector stub loads its selector and branches to that existing stub, removing the second GOT atom. It adds one branch to Objective-C dispatch but removes the unstable choice.

The long-term fast-mode fix belongs in Apple’s linker: give equal-name atoms a deterministic secondary order. Pass `-objc_stubs_small` to the affected links and keep strict binary and dSYM comparisons.

## Affected projects

- Animeko
