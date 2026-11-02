"""Normalize ELF binaries for production-content comparison."""

import hashlib
from pathlib import Path
import subprocess


def is_binary(file_header: bytes) -> bool:
    """Recognize ELF binaries from file_header's magic bytes."""
    return file_header[:4] == b"\x7fELF"


def compute_production_md5(product_path: Path, scratch_directory: Path) -> str:
    """Hash a normalized copy of product_path in scratch_directory using llvm-strip.

    Remove symbols, debug information, and the build ID without modifying the original.
    """
    stripped_path = scratch_directory / "elf-product"
    subprocess.run(["llvm-strip", "--strip-all", "--remove-section=.note.gnu.build-id", "-o", str(stripped_path), str(product_path)], check=True, capture_output=True, text=True)
    with stripped_path.open("rb") as output:
        return hashlib.file_digest(output, "md5").hexdigest()
