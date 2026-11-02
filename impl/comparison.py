"""Compare final products; keep platform-specific normalization in one function."""

from pathlib import Path
import hashlib
import subprocess
import tempfile

from impl.collection import inside, normalized_text, snapshot


def products(result: dict) -> dict[str, Path]:
    """Find final product files while result's cwd still points to the active project.

    result supplies cwd and link.output. Return logical project paths mapped to physical
    files beside the output; require Linux ELF, a header for .so, and no file symlinks.
    """
    project = Path(result["cwd"])
    output = Path(result["link"]["output"])
    if not inside(output, project) or not output.is_file():
        raise ValueError("Expected a Linux product file inside the project")
    with output.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError("Only Linux ELF products are supported; iOS is not implemented")
    files = {}
    for path in sorted(output.parent.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Product symlinks are unsupported: {path}")
        if path.is_file():
            files[str(Path("project") / path.relative_to(project))] = path
    if output.suffix == ".so" and not any(p.suffix == ".h" for p in files.values()):
        raise ValueError("The shared library's generated header is missing")
    return files


def production_digest(path: Path, temporary: Path) -> str:
    """Return the MD5 of path's production view without changing the original file.

    path is a product file. temporary is an existing scratch directory for a stripped
    ELF copy without its build ID. Normalize headers as text; hash other files unchanged.
    """
    if path.suffix == ".h":
        return hashlib.md5(normalized_text(path.read_bytes(), path.name)).hexdigest()
    with path.open("rb") as stream:
        elf = stream.read(4) == b"\x7fELF"
    if elf:
        output = temporary / "product"
        subprocess.run(["llvm-strip", "--strip-all", "--remove-section=.note.gnu.build-id", "-o", str(output), str(path)], check=True, capture_output=True, text=True)
        path = output
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def collect_products(result: dict, destination: Path):
    """Add artifact hashes and mtimes to result while its project directory is active.

    result supplies cwd and link.output and receives artifacts in place. destination is
    the run directory passed to snapshot; no product contents are saved at this stage.
    Raw records remain available if computing a production digest fails.
    """
    files = products(result)
    result["artifacts"] = [snapshot(path, logical, destination / "store", save=False) for logical, path in files.items()]
    with tempfile.TemporaryDirectory(prefix="ic-compare-") as temporary:
        for entry in result["artifacts"]:
            entry["production_md5"] = production_digest(files[entry["path"]], Path(temporary))


def save_artifacts(results: dict, destination: Path):
    """Save recorded original products after worktrees return to baseline/sut locations.

    results maps variant names to records containing logical artifact paths. destination
    contains those worktrees and the shared store where contents are deduplicated.
    """
    # This is the only place that resolves recorded paths after the worktrees move back.
    for name, result in results.items():
        for entry in result.get("artifacts", []):
            path = destination / name / Path(entry["path"]).relative_to("project")
            snapshot(path, entry["path"], destination / "store", save=True)


def compare(results: dict, destination: Path) -> dict:
    """Compare baseline/sut artifact records and save original products on strict differences.

    Args:
        results: Variant records with artifact paths, raw MD5s, and production MD5s.
        destination: Run directory containing parked worktrees and the shared store.

    Return strict_equal and production_equal, plus differing path lists on strict
    differences. Missing files count as differences; equal records require no file reads.
    """
    baseline, sut = ({f["path"]: f for f in results[name]["artifacts"]} for name in ("baseline", "sut"))
    missing = baseline.keys() ^ sut.keys()
    common = baseline.keys() & sut.keys()
    strict_diff = sorted(missing | {p for p in common if baseline[p]["md5"] != sut[p]["md5"]})
    production_diff = sorted(missing | {p for p in common if baseline[p]["production_md5"] != sut[p]["production_md5"]})
    comparison = dict(strict_equal=not strict_diff, production_equal=not production_diff)
    if strict_diff:
        save_artifacts(results, destination)
        comparison.update(strict_diff=strict_diff, production_diff=production_diff)
    return comparison
