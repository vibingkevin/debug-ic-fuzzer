"""Compare final products; keep platform-specific normalization in one function."""

from pathlib import Path
import hashlib
import subprocess
import tempfile

from impl.collection import is_within_directory, normalize_text_content, snapshot_file


def find_product_files(build_result: dict) -> dict[str, Path]:
    """Find final product files while build_result's cwd points to the active project.

    build_result supplies cwd and link.output. Return recorded project paths mapped to physical
    files beside the output; require Linux ELF, a header for .so, and no file symlinks.
    """
    project = Path(build_result["cwd"])
    output = Path(build_result["link"]["output"])
    if not is_within_directory(output, project) or not output.is_file():
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


def compute_production_md5(product_path: Path, scratch_directory: Path) -> str:
    """Return the MD5 of product_path's production view without changing the original file.

    product_path is the product file. scratch_directory holds a temporary stripped ELF
    copy without its build ID. Normalize headers as text; hash other files unchanged.
    """
    if product_path.suffix == ".h":
        return hashlib.md5(normalize_text_content(product_path.read_bytes(), product_path.name)).hexdigest()
    with product_path.open("rb") as stream:
        elf = stream.read(4) == b"\x7fELF"
    if elf:
        output = scratch_directory / "product"
        subprocess.run(["llvm-strip", "--strip-all", "--remove-section=.note.gnu.build-id", "-o", str(output), str(product_path)], check=True, capture_output=True, text=True)
        product_path = output
    with product_path.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def collect_product_hashes(build_result: dict):
    """Add artifact hashes and mtimes to build_result while its project directory is active.

    build_result supplies cwd and link.output and receives artifacts in place.
    No product contents are saved at this stage.
    Raw records remain available if computing a production digest fails.
    """
    files = find_product_files(build_result)
    build_result["artifacts"] = [snapshot_file(path, logical) for logical, path in files.items()]
    with tempfile.TemporaryDirectory(prefix="ic-compare-") as temporary:
        for entry in build_result["artifacts"]:
            entry["production_md5"] = compute_production_md5(files[entry["path"]], Path(temporary))


def save_product_contents(variant_results: dict):
    """Save recorded original products after worktrees return to baseline/sut locations.

    variant_results maps baseline/sut to records containing cwd and artifact paths.
    Each cwd's parent locates the run directory, parked worktrees, and shared content store.
    """
    # This is the only place that resolves recorded paths after the worktrees move back.
    for variant, build_result in variant_results.items():
        run_directory = Path(build_result["cwd"]).parent
        for entry in build_result.get("artifacts") or []:
            path = run_directory / variant / Path(entry["path"]).relative_to("project")
            snapshot_file(path, entry["path"], run_directory / "store")


def compare_products(variant_results: dict) -> dict:
    """Compare baseline/sut artifact records and save original products on strict differences.

    Args:
        variant_results: Baseline/sut records with task state, cwd, artifact paths, and both MD5s.

    Return strict_equal and production_equal, plus differing path lists on strict
    differences. Both UP-TO-DATE skips comparison with null equality fields; only one
    UP-TO-DATE saves available products and returns an error. Missing files count as
    differences; equal records require no file reads.
    """
    up_to_date = [result["task_state"] == "UP-TO-DATE" for result in variant_results.values()]
    if any(up_to_date):
        if all(up_to_date):
            return dict(strict_equal=None, production_equal=None, skipped="Both Native links are UP-TO-DATE")
        save_product_contents(variant_results)
        return {"error": "Native link execution states differ: only one variant is UP-TO-DATE"}
    baseline, sut = ({f["path"]: f for f in variant_results[name]["artifacts"]} for name in ("baseline", "sut"))
    missing = baseline.keys() ^ sut.keys()
    common = baseline.keys() & sut.keys()
    strict_diff = sorted(missing | {p for p in common if baseline[p]["md5"] != sut[p]["md5"]})
    production_diff = sorted(missing | {p for p in common if baseline[p]["production_md5"] != sut[p]["production_md5"]})
    comparison = dict(strict_equal=not strict_diff, production_equal=not production_diff)
    if strict_diff:
        save_product_contents(variant_results)
        comparison.update(strict_diff=strict_diff, production_diff=production_diff)
    return comparison
