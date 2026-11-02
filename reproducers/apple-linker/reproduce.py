"""Repeat an iOS device link using only Apple tools and a five-line assembly input."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def xcrun(*arguments: str) -> str:
    """Return xcrun output for arguments, using the selected DEVELOPER_DIR."""
    return subprocess.check_output(["xcrun", *arguments], text=True).strip()


def reproduce(output_directory: Path) -> None:
    """Create output_directory and record 100 identical-input links per library order.

    Save commands, input hashes, linker diagnostics, and one binary/symbol listing
    per distinct raw MD5. Compilation/link errors raise; a single observed output
    does not prove that the linker bug is absent.
    """
    output_directory.mkdir(parents=True, exist_ok=False)
    output_directory = output_directory.resolve()
    clang = xcrun("--find", "clang")
    linker = xcrun("--find", "ld")
    sdk = xcrun("--sdk", "iphoneos", "--show-sdk-path")
    resource_directory = subprocess.check_output([clang, "--print-resource-dir"], text=True).strip()
    runtime = str(Path(resource_directory) / "lib/darwin/libclang_rt.ios.a")
    source = Path(__file__).with_name("stack-probe.s").resolve()
    object_file = output_directory / "stack-probe.o"
    product = output_directory / "stack-probe.dylib"
    compile_command = [clang, "-target", "arm64-apple-ios15.0", "-isysroot", sdk, "-c", str(source), "-o", str(object_file)]
    subprocess.run(compile_command, check=True)
    # Keep the platform metadata from the failing Kotlin 2.4.21 invocation.
    base = [linker, "-dylib", "-arch", "arm64", "-platform_version", "ios", "15.0", "26.4", "-syslibroot", sdk, str(object_file), "-o", str(product), "-dead_strip"]
    libraries = ["-weak_framework", "CoreML", "-ldl", "-lresolv"]
    for framework in ("CoreFoundation", "Foundation", "CoreGraphics", "Metal", "QuartzCore", "UIKit", "IOSurface"):
        libraries.extend(["-framework", framework])
    libraries.append("-lobjc")
    for framework in ("UniformTypeIdentifiers", "CoreServices", "Photos", "PhotosUI"):
        libraries.extend(["-framework", framework])
    libraries.append("-lz")
    for framework in ("Security", "AVFAudio", "MediaPlayer", "CoreMedia", "AVFoundation", "Network", "WebKit"):
        libraries.extend(["-framework", framework])
    inputs = [source, object_file, Path(linker), Path(runtime)]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    records = []
    environment = {**os.environ, "ZERO_AR_DATE": "1"}
    for mode, ordered_libraries in (("system_first", ["-lSystem", runtime, *libraries]), ("runtime_first", [runtime, "-lSystem", *libraries])):
        command = base + ordered_libraries
        hashes = Counter()
        for _ in range(100):
            with (output_directory / f"{mode}.log").open("w") as log:
                subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
            content = product.read_bytes()
            digest = hashlib.md5(content).hexdigest()
            hashes[digest] += 1
            sample = output_directory / f"{digest}.dylib"
            if not sample.exists():
                sample.write_bytes(content)
                symbols = subprocess.check_output(["xcrun", "nm", "-m", str(sample)], text=True)
                sample.with_suffix(".symbols.txt").write_text(symbols)
        records.append(dict(mode=mode, command=command, raw_md5_counts=dict(hashes)))
        print(f"{mode}: {dict(hashes)}", flush=True)
        (output_directory / "results.json").write_text(json.dumps(dict(compile_command=compile_command, input_sha256=before, records=records), indent=2) + "\n")
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    if before != after:
        raise RuntimeError("Link inputs changed during reproduction")
    print("Input hashes unchanged. Inspect results.json and *.symbols.txt.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 reproduce.py NEW_OUTPUT_DIRECTORY")
    reproduce(Path(sys.argv[1]))
