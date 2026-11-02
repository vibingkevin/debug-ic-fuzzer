import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess

from macholib.MachO import MachO
from macholib.ptypes import sizeof
from macholib.mach_o import LC_UUID
import pytest

from impl.comparison import collect_product_hashes, compare_products


pytestmark = pytest.mark.skipif(platform.system() != "Darwin", reason="Requires Xcode's Mach-O tools")


def build_framework_fixture(framework_directory: Path, static: bool, value: int, debug_prefix: str | None = None):
    """Build a C framework binary in framework_directory with value as its function result.

    static selects an archive instead of a dynamic library. Keep source/object paths fixed
    across recompilations so code changes can be tested independently of debug paths.
    debug_prefix optionally remaps source paths in DWARF without changing executable code.
    """
    source = framework_directory.parent.parent.parent / "source.c"
    source.write_text(f"int value(void) {{ return {value}; }}\n")
    object_file = source.with_suffix(".o")
    debug_flags = [f"-fdebug-prefix-map={source.parent}={debug_prefix}"] if debug_prefix else []
    subprocess.run(["xcrun", "clang", "-g", *debug_flags, "-c", str(source), "-o", str(object_file)], check=True, capture_output=True)
    binary = framework_directory / framework_directory.stem
    command = ["xcrun", "libtool", "-static", "-o", str(binary), str(object_file)] if static else ["xcrun", "clang", "-dynamiclib", str(object_file), "-Wl,-install_name,@rpath/Sample.framework/Sample", "-o", str(binary)]
    subprocess.run(command, env={**os.environ, "ZERO_AR_DATE": "1"}, check=True, capture_output=True)


@pytest.mark.parametrize("static", [False, True])
@pytest.mark.parametrize("change", ["none", "debug", "debug-path", "missing-dsym", "code", "header", "modulemap", "plist", "identity"])
def test_framework_comparison(tmp_path, static, change):
    """Compare real framework binaries under tmp_path for the selected static mode and change.

    Debug-only changes must remain nonblocking; code and API/resources must differ in
    production. identity changes a Mach-O UUID or an archive member timestamp.
    """
    baseline = tmp_path / "baseline/build/Sample.framework"
    (baseline / "Headers").mkdir(parents=True)
    (baseline / "Modules").mkdir()
    (baseline / "Headers/Sample.h").write_text("int value(void);\n")
    (baseline / "Modules/module.modulemap").write_text('framework module Sample { umbrella header "Sample.h" export * }\n')
    (baseline / "Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": "1"}))
    build_framework_fixture(baseline, static, 1)
    dsym = baseline.parent / "Sample.framework.dSYM/Contents/Resources/DWARF"
    dsym.mkdir(parents=True)
    (dsym / "Sample").write_bytes(b"debug info")
    shutil.copytree(tmp_path / "baseline", tmp_path / "sut")
    sut = tmp_path / "sut/build/Sample.framework"
    if change == "debug":
        (sut.parent / "Sample.framework.dSYM/Contents/Resources/DWARF/Sample").write_bytes(b"different debug info")
    elif change == "missing-dsym":
        shutil.rmtree(sut.parent / "Sample.framework.dSYM")
    elif change == "code":
        build_framework_fixture(sut, static, 2)
    elif change == "debug-path":
        build_framework_fixture(sut, static, 1, debug_prefix="/different-source-path")
    elif change == "header":
        (sut / "Headers/Sample.h").write_text("long value(void);\n")
    elif change == "modulemap":
        (sut / "Modules/module.modulemap").write_text("framework module Different {}\n")
    elif change == "plist":
        (sut / "Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": "2"}))
    elif change == "identity":
        binary = sut / "Sample"
        if static:
            content = bytearray(binary.read_bytes())
            content[24:36] = b"1234567890  "
            binary.write_bytes(content)
        else:
            subprocess.run(["codesign", "--remove-signature", str(binary)], check=True, capture_output=True)
            macho = MachO(str(binary))
            with binary.open("r+b") as output:
                for header in macho.headers:
                    command_offset = header.offset + sizeof(header.header)
                    for command, _, _ in header.commands:
                        if command.cmd == LC_UUID:
                            output.seek(command_offset + sizeof(command))
                            output.write(b"x" * 16)
                        command_offset += command.cmdsize
    original_contents = (sut / "Sample").read_bytes()
    results = {}
    for variant in ("baseline", "sut"):
        parked = tmp_path / variant
        active = tmp_path / "project"
        parked.rename(active)
        try:
            result = dict(cwd=str(active), task_state="EXECUTED", link={"output": str(active / "build/Sample.framework")})
            collect_product_hashes(result)
            results[variant] = result
        except subprocess.CalledProcessError as error:
            pytest.fail(error.stderr)
        finally:
            active.rename(parked)
    comparison = compare_products(results)
    # A dynamic link may already omit the remapped DWARF present in its input object.
    if change != "debug-path":
        assert comparison["strict_equal"] == (change == "none")
    assert comparison["production_equal"] == (change in ("none", "debug", "debug-path", "missing-dsym", "identity"))
    assert (sut / "Sample").read_bytes() == original_contents
    assert (tmp_path / "store").exists() == (not comparison["strict_equal"])
