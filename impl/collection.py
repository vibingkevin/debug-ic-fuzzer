"""Collect cache snapshots and deduplicate saved contents within a run."""

from pathlib import Path
import hashlib


CACHE_TEXT = {"metadata.properties", "bitcode_deps"}


def normalized_text(data: bytes, name: str) -> bytes:
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if name == "metadata.properties":
        text = "\n".join(line for line in text.splitlines()
                         if line.split("=", 1)[0].strip() != "compilerFingerprint")
    return text.strip().encode("utf-8")


def snapshot(path: Path, logical_path: str, store: Path, save: bool, text: bool = False) -> dict:
    data = path.read_bytes() if save or text else None
    if text:
        data = normalized_text(data, path.name)
    if data is None:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "md5").hexdigest()
    else:
        digest = hashlib.md5(data).hexdigest()
    if save:
        saved = store / path.name / f"{path.name}-{digest}"
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            saved.write_bytes(data)
    return dict(path=logical_path, md5=digest, mtime_ns=path.stat().st_mtime_ns)


def inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def collect(result: dict, destination: Path):
    """Collect while all compiler-reported project paths still exist."""
    files = result["cache_files"] = []
    link = result["link"]
    project = Path(result["cwd"])
    native = Path(result["native_home"]) / "klib/cache"
    roots = [(native, "native-home/klib/cache")]
    options = [line.strip() for line in (destination / result["log"]).read_text(errors="replace").splitlines()]
    if link is not None:
        if result["task_state"] != "UP-TO-DATE":
            directories = {s.split("=", 1)[1] for s in options if s.startswith("-Xic-cache-dir=")}
            if len(directories) == 1:
                link["ic_cache_dir"] = directories.pop()
        if "ic_cache_dir" in link:
            ic = Path(link["ic_cache_dir"])
            if not ic.is_relative_to(project):
                raise ValueError(f"IC cache is outside the project: {ic}")
            if not inside(ic, project):
                raise ValueError(f"IC cache escapes the worktree: {ic}")
            roots.append((ic, str(Path("project") / ic.relative_to(project))))
        elif result["exit_code"] == 0:
            raise ValueError("Expected one -Xic-cache-dir for the final link")

    def checked_cache(path: str):
        actual = Path(path)
        if not any(inside(actual, root) for root, _ in roots):
            raise ValueError(f"Cache path is outside tracked roots: {path}")

    # Inspect explicit cache overrides as well as archives reported by the compiler.
    for arg in options:
        if arg.startswith(("-Xauto-cache-dir=", "-Xcache-directory=")):
            checked_cache(arg.split("=", 1)[1])
        elif arg.startswith("-Xcached-library="):
            checked_cache(arg.split("=", 1)[1].split(",", 1)[1])
    if link and result["task_state"] != "UP-TO-DATE" and "built_caches" in link:
        report = Path(link["built_caches"])
        if not report.is_relative_to(project):
            raise ValueError("Built-cache report is outside the project")
        if report.exists():
            for archive in report.read_text().splitlines():
                if archive:
                    checked_cache(archive)
        elif result["exit_code"] == 0:
            raise ValueError("The compiler did not write its built-cache report")
    for root, logical in roots:
        if root.is_symlink():
            raise ValueError(f"Cache root symlinks are unsupported: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Cache symlinks are unsupported: {path}")
            if path.is_file():
                text = path.name in CACHE_TEXT
                files.append(snapshot(path, str(Path(logical) / path.relative_to(root)),
                                      destination / "store", save=text, text=text))
