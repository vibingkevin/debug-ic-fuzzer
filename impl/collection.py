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


def collect(roots: list[tuple[Path, str]], store: Path) -> list[dict]:
    """Snapshot the supplied directories without knowing about builds or task states."""
    files = []
    for root, logical in roots:
        if root.is_symlink():
            raise ValueError(f"Cache root symlinks are unsupported: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Cache symlinks are unsupported: {path}")
            if path.is_file():
                text = path.name in CACHE_TEXT
                files.append(snapshot(path, str(Path(logical) / path.relative_to(root)),
                                      store, save=text, text=text))
    return files
