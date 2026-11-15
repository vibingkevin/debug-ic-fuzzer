#!/usr/bin/env python3
"""Reproduce Kotlin/Native inline DWARF sensitivity to cache rebuild scope."""

from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


TARGET = "ios_arm64"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def run(command: list[str], cwd: Path, env: dict[str, str], label: str) -> None:
    with (cwd / f"{label}.log").open("w") as log:
        subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)


def tool(home: Path, name: str) -> str:
    for candidate in (home / "konan/bin" / name, home / "bin" / name):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise SystemExit(f"cannot find {name}; set a complete KONAN_HOME")


def cache_debug(home: Path, cache_root: Path, output: Path) -> str:
    ar = tool(home, "llvm-ar")
    dwarfdump = tool(home, "llvm-dwarfdump")
    output.write_text("")
    with tempfile.TemporaryDirectory(prefix="inline-dwarf-object-") as directory:
        temp = Path(directory)
        for archive in sorted(cache_root.rglob("*.a")):
            members = subprocess.run([ar, "t", str(archive)], text=True, capture_output=True, check=False)
            if members.returncode:
                continue
            for member in members.stdout.splitlines():
                obj = temp / f"{len(list(temp.iterdir()))}.o"
                with obj.open("wb") as extracted:
                    subprocess.run([ar, "p", str(archive), member], stdout=extracted, check=True)
                dump = subprocess.run(
                    [dwarfdump, "--debug-info", str(obj)], text=True, capture_output=True, check=False
                )
                if "A.kt" in dump.stdout or "B.kt" in dump.stdout or "Wrapped.kt" in dump.stdout:
                    output.write_text(dump.stdout)
                    return dump.stdout
    raise RuntimeError("no cache object with the expected inline DWARF was found")


def main() -> None:
    home_value = os.environ.get("KONAN_HOME")
    if not home_value:
        raise SystemExit("set KONAN_HOME to a Kotlin/Native 2.4.21 distribution")
    home = Path(home_value).expanduser().resolve()
    konanc = tool(home, "konanc")

    with tempfile.TemporaryDirectory(prefix="inline-dwarf-") as directory:
        root = Path(directory)
        source = root / "source"
        external = root / "external"
        project = root / "project"
        ic = root / "ic"
        auto = root / "auto"
        output = root / "out"
        for path in (source, external, project, ic, auto, output):
            path.mkdir()

        (source / "Wrapped.kt").write_text(
            "package external\n"
            + ("/* inline template padding */\n" * 256)
            + "inline fun wrapped(block: () -> Int): Int = 1.let { block() + it }\n"
        )
        (source / "A.kt").write_text(
            "package project\nimport external.wrapped\nfun a(x: Int): Int = wrapped { x + 2 }\n"
        )
        caller_b = source / "B.kt"
        caller_b.write_text(
            "package project\nimport external.wrapped\n"
            + ("/* caller padding */\n" * 64)
            + "fun b(x: Int): Int = wrapped { x + 2 }\n"
        )

        environment = {**os.environ, "KONAN_DATA_DIR": str(root / "konan-data")}
        external_klib = external / "Wrapped.klib"
        project_klib = project / "Project.klib"
        run(
            [konanc, str(source / "Wrapped.kt"), "-target", TARGET, "-g", "-produce", "library",
             "-module-name", "Wrapped", "-o", str(external_klib)],
            root,
            environment,
            "compile-external",
        )

        def compile_project(label: str) -> None:
            run(
                [konanc, str(source / "A.kt"), str(source / "B.kt"), "-target", TARGET, "-g",
                 "-produce", "library", "-module-name", "Project", "-l", str(external_klib),
                 "-o", str(project_klib)],
                root,
                environment,
                label,
            )

        compile_project("compile-project-1")

        def link(label: str) -> tuple[str, str]:
            binary_prefix = output / "InlineDwarfMini"
            for path in output.glob("InlineDwarfMini*"):
                path.unlink()
            command = [
                konanc,
                "-target", TARGET,
                "-g",
                "-produce", "dynamic",
                "-module-name", "InlineDwarfMini",
                "-Xinclude=" + str(project_klib),
                "-l", str(external_klib),
                "-Xpartial-linkage=enable",
                "-Xpartial-linkage-loglevel=error",
                "-Xenable-incremental-compilation",
                "-Xbackend-threads=1",
                "-Xauto-cache-from=" + str(external),
                "-Xauto-cache-dir=" + str(auto),
                "-Xic-cache-dir=" + str(ic),
                "-o", str(binary_prefix),
            ]
            run(command, root, environment, label)
            binaries = [p for p in output.glob("InlineDwarfMini*") if p.is_file()]
            if not binaries:
                raise RuntimeError(f"link produced no binary; inspect {root / (label + '.log')}")
            binary = max(binaries, key=lambda path: path.stat().st_size)
            debug = root / f"{label}.dwarf.txt"
            text = cache_debug(home, ic, debug)
            return digest(binary), sha256(text.encode()).hexdigest()

        full, full_debug = link("full")
        repeat, repeat_debug = link("repeat")
        if (full, full_debug) != (repeat, repeat_debug):
            raise SystemExit("the full-cache control was not stable")

        caller_b.write_text(caller_b.read_text().replace("x + 2", "x + 3"))
        compile_project("compile-project-2")
        incremental, incremental_debug = link("incremental")

        saved_ic = root / "ic-incremental"
        ic.rename(saved_ic)
        ic.mkdir()
        clean, clean_debug = link("project-ic-clean")
        print(f"incremental binary: {incremental}")
        print(f"clean binary:       {clean}")
        print(f"incremental DWARF:  {incremental_debug}")
        print(f"clean DWARF:        {clean_debug}")

        if incremental == clean and incremental_debug != clean_debug:
            print("reproduced: binary is equal but inline DWARF differs")
            return
        raise SystemExit("the selected Kotlin/Native distribution did not reproduce the expected split")


if __name__ == "__main__":
    main()
