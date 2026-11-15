"""Normalize Mach-O binaries and static framework archives for production-content comparison."""

import hashlib
import os
from pathlib import Path
import subprocess

from macholib.MachO import MachO
from macholib.ptypes import sizeof
from macholib.mach_o import FAT_CIGAM, FAT_MAGIC, LC_CODE_SIGNATURE, LC_UUID, MH_CIGAM, MH_CIGAM_64, MH_MAGIC, MH_MAGIC_64


def is_binary(file_header: bytes) -> bool:
    """Recognize Mach-O binaries and static archives from file_header's first eight bytes."""
    return file_header == b"!<arch>\n" or int.from_bytes(file_header[:4], "big") in {MH_MAGIC, MH_CIGAM, MH_MAGIC_64, MH_CIGAM_64, FAT_MAGIC, FAT_CIGAM}


def compute_production_md5(product_path: Path, scratch_directory: Path) -> str:
    """Hash a normalized copy of product_path in scratch_directory using Xcode tools.

    Keep static archive symbols/relocations needed for linking; remove debug information
    and normalize archive timestamps. For dynamic Mach-O also discard local symbols,
    remove the code signature, and zero UUIDs using macholib. Never modify the original.
    """
    with product_path.open("rb") as source:
        archive = source.read(8) == b"!<arch>\n"
    stripped_path = scratch_directory / "macho-product"
    flags = ["-S"] if archive else ["-S", "-x"]
    subprocess.run(["xcrun", "strip", *flags, "-o", str(stripped_path), str(product_path)], env={**os.environ, "ZERO_AR_DATE": "1"}, check=True, capture_output=True, text=True)
    if not archive:
        macho = MachO(str(stripped_path))
        if any(command.cmd == LC_CODE_SIGNATURE for header in macho.headers for command, _, _ in header.commands):
            subprocess.run(["codesign", "--remove-signature", str(stripped_path)], check=True, capture_output=True, text=True)
            macho = MachO(str(stripped_path))
        with stripped_path.open("r+b") as output:
            for header in macho.headers:
                command_offset = header.offset + sizeof(header.header)
                for command, _, _ in header.commands:
                    if command.cmd == LC_UUID:
                        output.seek(command_offset + sizeof(command))
                        output.write(bytes(16))
                    command_offset += command.cmdsize
    with stripped_path.open("rb") as output:
        return hashlib.file_digest(output, "md5").hexdigest()
