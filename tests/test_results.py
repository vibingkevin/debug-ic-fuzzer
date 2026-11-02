from pathlib import Path
import shutil
import subprocess

import pytest

from impl.collection import collect_cache_files
from impl.comparison import collect_product_hashes, compare_products, save_product_contents


@pytest.fixture
def product_records(tmp_path):
    """Build identical baseline/sut ELF libraries and headers under tmp_path; return link records."""
    source = tmp_path / "source.c"
    results = {}
    for name in ("baseline", "sut"):
        output = tmp_path / name / "build/libdemo.so"
        output.parent.mkdir(parents=True)
        source.write_text("int value(void) { return 1; }\n")
        subprocess.run(["cc", "-g", "-shared", "-fPIC", str(source), "-o", str(output)], check=True)
        (output.parent / "libdemo_api.h").write_text("int value(void);\n")
        results[name] = dict(variant=name, task_state="EXECUTED", cwd=str(tmp_path / "project"), link={"output": str(tmp_path / "project/build/libdemo.so")})
    # Start with byte-identical product_records, independent of the host linker's defaults.
    shutil.copyfile(tmp_path / "baseline/build/libdemo.so", tmp_path / "sut/build/libdemo.so")
    return results


def collect_pair_products(variant_results):
    """Collect variant_results through each recorded cwd, restoring parked directories afterward."""
    for variant, result in variant_results.items():
        active = Path(result["cwd"])
        parked = active.parent / variant
        parked.rename(active)
        try:
            collect_product_hashes(result)
        finally:
            active.rename(parked)


def test_equal_products_keep_hashes_without_contents(tmp_path, product_records):
    """Verify identical product records compare without saved files in tmp_path."""
    collect_pair_products(product_records)
    # Equal results need no access to the parked product_records.
    shutil.rmtree(tmp_path / "baseline")
    shutil.rmtree(tmp_path / "sut")
    assert compare_products(product_records) == dict(strict_equal=True, production_equal=True)
    assert not (tmp_path / "store").exists()
    for result in product_records.values():
        assert len(result["artifacts"]) == 2
        binary = next(f for f in result["artifacts"] if f["path"].endswith(".so"))
        assert len(binary["md5"]) == len(binary["production_md5"]) == 32
        assert binary["md5"] != binary["production_md5"]


@pytest.mark.parametrize("change", ["debug", "build-id", "code", "header", "missing"])
def test_product_differences(tmp_path, product_records, change):
    """Mutate product_records under tmp_path and check comparison flags and saved original bytes.

    change selects a debug, build-ID, code, header, or file-set difference.
    """
    output = tmp_path / "sut/build/libdemo.so"
    if change == "debug":
        data = tmp_path / "debug-data"
        data.write_bytes(b"different debug information")
        subprocess.run(["objcopy", f"--add-section=.debug_test={data}", str(output)], check=True)
    elif change == "build-id":
        data = tmp_path / "note"
        subprocess.run(["objcopy", f"--dump-section=.note.gnu.build-id={data}", str(output)], check=True)
        content = bytearray(data.read_bytes())
        content[-1] ^= 1
        data.write_bytes(content)
        subprocess.run(["objcopy", f"--update-section=.note.gnu.build-id={data}", str(output)], check=True)
    elif change == "code":
        source = tmp_path / "source.c"
        source.write_text("int value(void) { return 2; }\n")
        subprocess.run(["cc", "-g", "-shared", "-fPIC", str(source), "-o", str(output)], check=True)
    elif change == "header":
        (output.parent / "libdemo_api.h").write_text("long value(void);\n")
    else:
        (output.parent / "extra.h").write_text("int extra(void);\n")
    collect_pair_products(product_records)
    result = compare_products(product_records)
    assert result["strict_equal"] is False
    assert result["production_equal"] == (change in ("debug", "build-id"))
    for record in product_records.values():
        for file in record["artifacts"]:
            name = Path(file["path"]).name
            saved = tmp_path / "store" / name / f"{name}-{file['md5']}"
            original = tmp_path / record["variant"] / Path(file["path"]).relative_to("project")
            assert saved.read_bytes() == original.read_bytes()
            assert set(file) == {"path", "md5", "mtime_ns", "production_md5"}


def test_header_whitespace_is_nonblocking_but_originals_are_saved(tmp_path, product_records):
    """Alter product_records fixture header whitespace in tmp_path; require equal production hashes and saved originals."""
    (tmp_path / "sut/build/libdemo_api.h").write_bytes(b" \r\nint value(void);\r\n")
    collect_pair_products(product_records)
    result = compare_products(product_records)
    assert not result["strict_equal"] and result["production_equal"]
    assert len(list((tmp_path / "store/libdemo_api.h").iterdir())) == 2


def test_cache_snapshot_normalization_dedup_and_no_binary_contents(tmp_path):
    """Create caches in tmp_path and verify normalized text deduplication and hash-only binary records."""
    root = tmp_path / "project/build/ic"
    root.mkdir(parents=True)
    metadata = root / "metadata.properties"
    metadata.write_bytes(b" hash=abc\r\ncompilerFingerprint=old\r\n")
    (root / "bin").mkdir()
    archive = root / "bin/code.a"
    archive.write_bytes(b"archive")
    (root / "inline_bodies").write_bytes(b"\x00\x01")
    native = tmp_path / "native/klib/cache"
    native.mkdir(parents=True)
    (native / "bitcode_deps").write_text("stdlib\n")
    (native / "bin").mkdir()
    native_archive = native / "bin/stdlib.a"
    native_archive.write_bytes(b"stdlib")
    report = tmp_path / "built-caches.txt"
    report.write_text(f"{archive}\n{native_archive}\n")
    record = dict(cwd=str(tmp_path / "project"), native_home=str(tmp_path / "native"), variant="baseline", exit_code=0, link={"built_caches": str(report)})
    files = collect_cache_files(record, tmp_path / "store")
    first = next(f for f in files if f["path"].endswith("metadata.properties"))
    metadata.write_text("hash=abc\ncompilerFingerprint=new\n")
    files = collect_cache_files(record, tmp_path / "store")
    second = next(f for f in files if f["path"].endswith("metadata.properties"))
    assert first["md5"] == second["md5"]
    assert len(list((tmp_path / "store/metadata.properties").iterdir())) == 1
    assert not (tmp_path / "store/code.a").exists()
    assert not (tmp_path / "store/inline_bodies").exists()
    assert len(files) == 5
    assert all(f["path"].startswith(("project/", "native-home/")) for f in files)


def test_strip_error_retains_hashes_and_can_save_after_restore(tmp_path, product_records, monkeypatch):
    """Use monkeypatch to fail stripping product_records; verify raw hashes and later saving under tmp_path."""

    def fail(*_):
        """Raise a simulated strip error; ignore all compute_production_md5 arguments in _."""
        raise OSError("strip unavailable")

    monkeypatch.setattr("impl.comparison.compute_production_md5", fail)
    with pytest.raises(OSError, match="strip unavailable"):
        collect_pair_products(product_records)
    assert not (tmp_path / "project").exists()
    assert len(product_records["baseline"]["artifacts"]) == 2
    save_product_contents(product_records)
    for f in product_records["baseline"]["artifacts"]:
        name = Path(f["path"]).name
        assert (tmp_path / "store" / name / f"{name}-{f['md5']}").is_file()


@pytest.mark.parametrize("skipped_variants", [("baseline", "sut"), ("baseline",), ("sut",)])
def test_up_to_date_comparison(tmp_path, product_records, skipped_variants):
    """Check skipped comparisons and saving of executed products under tmp_path.

    product_records supplies real ELF files; skipped_variants selects records without
    current link metadata or artifact hashes.
    """
    for variant in skipped_variants:
        product_records[variant].update(task_state="UP-TO-DATE", link=None, artifacts=None)
    executed = {variant: record for variant, record in product_records.items() if variant not in skipped_variants}
    collect_pair_products(executed)
    comparison = compare_products(product_records)
    if not executed:
        assert comparison == dict(strict_equal=None, production_equal=None, skipped="Both Native links are UP-TO-DATE")
        assert not (tmp_path / "store").exists()
    else:
        assert "only one variant is UP-TO-DATE" in comparison["error"]
        for variant, record in executed.items():
            for artifact in record["artifacts"]:
                filename = Path(artifact["path"]).name
                stored = tmp_path / "store" / filename / f"{filename}-{artifact['md5']}"
                original = tmp_path / variant / Path(artifact["path"]).relative_to("project")
                assert stored.read_bytes() == original.read_bytes()


def test_only_reported_cache_units_are_collected(tmp_path):
    """Select only reported units in tmp_path, including external paths and excluding unchanged siblings."""
    project = tmp_path / "project"
    compiler = tmp_path / "compiler"
    cache_locations = [tmp_path / "baseline-auto-cache", project / "module/build/ic", compiler / "klib/cache", tmp_path / "external-cache"]
    reported_archives = []
    for root in cache_locations:
        for unit in ("changed", "unchanged"):
            cache = root / unit
            (cache / "bin").mkdir(parents=True)
            archive = cache / "bin/libcache.a"
            archive.write_bytes(unit.encode())
            (cache / "metadata.properties").write_text("hash=abc")
            if unit == "changed":
                reported_archives.append(archive)
    report = project / "module/build/built-caches.txt"
    report.write_text("\n".join(str(path) for path in [*reported_archives, reported_archives[0]]))
    record = dict(cwd=str(project), native_home=str(compiler), variant="baseline", exit_code=0, link={"built_caches": str(report)})
    records = collect_cache_files(record, tmp_path / "store")
    assert record["rebuilt_cache_count"] == 4
    assert len(records) == 8
    assert all("/changed/" in entry["path"] for entry in records)
    assert any(entry["path"].startswith("native-home/") for entry in records)
    assert any(entry["path"].startswith("auto-cache/") for entry in records)
    assert any(entry["path"].startswith("project/module/build/ic/") for entry in records)
    assert any(entry["path"].startswith(str(tmp_path / "external-cache")) for entry in records)
    report.write_text("")
    assert collect_cache_files(record, tmp_path / "store") == []
    assert record["rebuilt_cache_count"] == 0
    report.unlink()
    with pytest.raises(FileNotFoundError):
        collect_cache_files(record, tmp_path / "store")
    record["exit_code"] = 1
    assert collect_cache_files(record, tmp_path / "store") == []
