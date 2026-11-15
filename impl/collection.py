"""Collect cache snapshots and deduplicate saved contents within a run."""

from pathlib import Path
import hashlib


CACHE_TEXT = {"metadata.properties", "bitcode_deps"}


def normalize_text_content(content: bytes, filename: str) -> bytes:
    """Normalize UTF-8 data to LF and strip surrounding whitespace.

    content is the UTF-8 file data; filename selects metadata.properties fingerprint removal.
    Return normalized UTF-8 bytes for both hashing and storage.
    """
    text = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if filename == "metadata.properties":
        text = "\n".join(line for line in text.splitlines() if line.split("=", 1)[0].strip() != "compilerFingerprint")
    return text.strip().encode("utf-8")


def snapshot_file(file_path: Path, recorded_path: str, store_directory: Path | None = None, *, normalize_text: bool = False) -> dict:
    """Hash a file and optionally save its contents without duplicating existing data.

    Args:
        file_path: Physical file to read and stat.
        recorded_path: Stable path written to the record instead of the physical location.
        store_directory: Content store root, or None to hash without saving contents.
        normalize_text: Normalize text before hashing/saving instead of using raw bytes.

    Returns:
        A record containing logical path, MD5, and the original file's mtime_ns.
    """
    data = file_path.read_bytes() if store_directory is not None or normalize_text else None
    if normalize_text:
        data = normalize_text_content(data, file_path.name)
    if data is None:
        with file_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "md5").hexdigest()
    else:
        digest = hashlib.md5(data).hexdigest()
    if store_directory is not None:
        saved = store_directory / file_path.name / f"{file_path.name}-{digest}"
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            saved.write_bytes(data)
    return dict(path=recorded_path, md5=digest, mtime_ns=file_path.stat().st_mtime_ns)


def is_within_directory(candidate_path: Path, parent_directory: Path) -> bool:
    """Return whether candidate_path resolves to parent_directory or a descendant, following symlinks."""
    return candidate_path.resolve().is_relative_to(parent_directory.resolve())


def collect_cache_files(build_result: dict, store_directory: Path) -> list[dict]:
    """Read the compiler's built-cache report and snapshot only its reported cache units.

    Args:
        build_result: Current link report, exit status, cwd, and Native home.
        store_directory: Shared content store for normalized metadata.properties and bitcode_deps.

    Set build_result's rebuilt_cache_count from unique reported archive paths before scanning.
    Return file records with paths, MD5s, and mtimes. Other files are hashed without
    saving contents. Failed builds may have no report; successful links must write one.
    Known directory prefixes normalize recorded paths without limiting collection scope.
    """
    link = build_result["link"]
    if link is None:
        return []
    report = Path(link["built_caches"])
    if not report.exists() and build_result["exit_code"] != 0:
        return []
    rebuilt_archives = {Path(archive) for archive in report.read_text().splitlines() if archive}
    build_result["rebuilt_cache_count"] = len(rebuilt_archives)
    cache_directories = sorted({archive.parent.parent for archive in rebuilt_archives})
    project_directory = Path(build_result["cwd"])
    path_prefixes = [(project_directory, "project"), (Path(build_result["native_home"]), "native-home")]
    files = []
    for cache_directory in cache_directories:
        recorded_directory = cache_directory
        for directory, prefix in path_prefixes:
            if cache_directory.is_relative_to(directory):
                recorded_directory = Path(prefix) / cache_directory.relative_to(directory)
                break
        for path in sorted(cache_directory.rglob("*")):
            if path.is_file():
                text = path.name in CACHE_TEXT
                files.append(snapshot_file(path, str(recorded_directory / path.relative_to(cache_directory)), store_directory if text else None, normalize_text=text))
    return files
