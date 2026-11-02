"""Build each selected commit with baseline and sut."""

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import argparse
import json
import subprocess
import tarfile

import yaml

from impl.build import build_project
from impl.collection import collect_cache_files
from impl.comparison import collect_product_hashes, compare_products, save_product_contents
from impl.config import Compilers, Project, load_config, resolve_native_home
from impl.repository import prepare_repository, create_detached_worktree, activate_worktree


def replay_commits(compilers_yaml: Path, project_yaml: Path) -> int:
    """Replay both compilers, collect results, and stop after a failed commit pair.

    Args:
        compilers_yaml: YAML file defining baseline and sut distributions.
        project_yaml: YAML file defining the repository, commit range, and Gradle task.

    Returns:
        0 if all pairs pass production comparison, otherwise 1. Writes results and
        worktrees under the current directory; configuration/setup errors propagate.
    """
    compilers = load_config(compilers_yaml, Compilers).compilers
    project_config = load_config(project_yaml, Project)
    project_config.gradle_args = [arg.replace("{config_dir}", str(project_yaml.parent)) for arg in project_config.gradle_args]
    tool_directory = Path.cwd()
    run_directory = tool_directory / "run" / f"{compilers_yaml.stem}-{project_yaml.stem}-{datetime.now():%Y%m%d-%H%M%S}"
    run_directory.mkdir(parents=True)
    with ExitStack() as compiler_cleanup:
        native_homes = {variant: resolve_native_home(compiler_config, compilers_yaml.parent, compiler_cleanup) for variant, compiler_config in compilers.items()}
        if (native_homes["baseline"] / "klib/cache").resolve() == (native_homes["sut"] / "klib/cache").resolve():
            raise ValueError("baseline and sut cannot use the same Native cache")
        bare_repository, commit_ids = prepare_repository(tool_directory / "repos", project_config)
        for variant in ("baseline", "sut"):
            create_detached_worktree(bare_repository, run_directory / variant, commit_ids[0])
        failed = False
        previous_links = {"baseline": None, "sut": None}
        for commit_index, commit_id in enumerate(commit_ids):
            variant_results = {}
            for variant in ("baseline", "sut"):
                record_stem = f"{commit_index:04d}-{commit_id[:12]}-{variant}"
                with activate_worktree(bare_repository, run_directory / variant, commit_id) as project_directory:
                    build_result, cache_roots = build_project(project_directory, project_config, compilers[variant], native_homes[variant], record_stem, previous_links[variant])
                    build_result.update(commit=commit_id, variant=variant)
                    try:
                        build_result["cache_files"] = collect_cache_files(cache_roots, run_directory / "store")
                        if build_result["exit_code"] == 0 and build_result["error"] is None:
                            collect_product_hashes(build_result)
                    except (ValueError, OSError, subprocess.CalledProcessError) as error:
                        build_result["error"] = build_result["error"] or str(error)
                    variant_results[variant] = build_result
                    (run_directory / f"{record_stem}.json").write_text(json.dumps(build_result, ensure_ascii=False, indent=2) + "\n")
                failed |= build_result["exit_code"] != 0 or build_result["error"] is not None
                previous_links[variant] = build_result["link"]
                print(f"{commit_index:04d} {commit_id[:12]} {variant}: exit={build_result['exit_code']} error={build_result['error']}", flush=True)
            comparison = {"error": "Build or collection failed"}
            try:
                if failed:
                    save_product_contents(variant_results)
                else:
                    comparison = compare_products(variant_results)
            except (ValueError, OSError) as error:
                comparison = {"error": str(error)}
            (run_directory / f"{commit_index:04d}-{commit_id[:12]}-comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
            print(f"{commit_index:04d} comparison: {comparison}", flush=True)
            failed |= comparison.get("production_equal") is not True
            if failed:
                break
        print(run_directory, flush=True)
        return int(failed)


def main():
    """Read CLI arguments and return the replay status; exit with 2 on setup errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compilers", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    args = parser.parse_args()
    try:
        return replay_commits(args.compilers.resolve(), args.project.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError, yaml.YAMLError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
