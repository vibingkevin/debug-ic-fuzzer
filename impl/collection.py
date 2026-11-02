"""Collect cache snapshots and deduplicate saved contents within a run."""

from pathlib import Path
import hashlib


CACHE_TEXT = {"metadata.properties", "bitcode_deps"}


def normalized_text(data: bytes, name: str) -> bytes:
    """Normalize UTF-8 data to LF and strip surrounding whitespace.

    name is the original filename; metadata.properties also loses compilerFingerprint.
    Return normalized UTF-8 bytes for both hashing and storage.
    """
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if name == "metadata.properties":
        text = "\n".join(line for line in text.splitlines() if line.split("=", 1)[0].strip() != "compilerFingerprint")
    return text.strip().encode("utf-8")


def snapshot(path: Path, logical_path: str, store: Path, save: bool, text: bool = False) -> dict:
    """Hash a file and optionally save its contents without duplicating existing data.

    Args:
        path: Physical file to read and stat.
        logical_path: Stable path recorded in the result instead of the physical location.
        store: Content store root, using <filename>/<filename>-<md5> for saved files.
        save: Whether to retain file contents in the store.
        text: Whether to normalize text before hashing and saving; otherwise use raw bytes.

    Returns:
        A record containing logical path, MD5, and the original file's mtime_ns.
    """
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
    """Return whether path resolves to root or a descendant, following symbolic links."""
    return path.resolve().is_relative_to(root.resolve())


def collect(roots: list[tuple[Path, str]], store: Path) -> list[dict]:
    """Snapshot cache files without interpreting builds or task states.

    Args:
        roots: (Physical directory, logical path prefix) pairs to scan recursively.
        store: Shared content store for normalized metadata.properties and bitcode_deps.

    Return file records with paths, MD5s, and mtimes. Other files are hashed without
    saving contents; missing roots contribute no files, and symbolic links are rejected.
    """
    files = []
    for root, logical in roots:
        if root.is_symlink():
            raise ValueError(f"Cache root symlinks are unsupported: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Cache symlinks are unsupported: {path}")
            if path.is_file():
                text = path.name in CACHE_TEXT
                files.append(snapshot(path, str(Path(logical) / path.relative_to(root)), store, save=text, text=text))
    return files
