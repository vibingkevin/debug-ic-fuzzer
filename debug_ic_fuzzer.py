"""命令行入口与 baseline / sut 逐提交回放。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tarfile

import yaml

from ic_build import Variant
from ic_config import Compilers, Invalid, Project, Projects, absolute, prepare_home
from ic_results import Store, differences, pair_status, write_json


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commits(repo: Path, config: Project) -> list[str]:
    # 先确定含两端的主线区间，reverse 只改变执行顺序。
    history = git(repo, "rev-list", "--first-parent", "--reverse", f"refs/remotes/origin/{config.branch}").splitlines()
    start = git(repo, "rev-parse", "--verify", config.start_commit + "^{commit}")
    end = git(repo, "rev-parse", "--verify", config.end_commit + "^{commit}")
    if start not in history or end not in history or history.index(start) > history.index(end):
        raise Invalid("开始提交和结束提交必须在所选分支上，且开始提交不能晚于结束提交。")
    selected = history[history.index(start):history.index(end) + 1]
    return selected if config.direction == "forward" else selected[::-1]


def replay(project, variants, revisions, destination, store):
    rows, failed = [], False
    for index, revision in enumerate(revisions):
        for variant in variants:
            git(variant.checkout, "checkout", "--detach", revision)
        folder = destination / f"{index:04d}-{revision[:12]}"
        row = dict(commit=revision, steps=[])
        rows.append(row)
        previous = None
        last = index == len(revisions) - 1
        for phase in (["clean"] if index == 0 else ["incremental", "clean"]):
            if previous and row["steps"][-1]["status"] == "PASS" and not last:
                break
            results = []
            for variant in variants:
                results.append(variant.build(project, folder / phase / variant.name, phase == "clean"))
                if results[-1]["status"] == "INVALID":
                    break
            status, diff = pair_status(results, store)
            step = dict(phase=phase, status=status, diff=diff)
            changed_after_clean = False
            if len(results) == 2:
                if previous:
                    step["incremental_vs_clean"] = {v.name: differences(old, new, store)
                        for v, old, new in zip(variants, previous, results) if old["status"] == "PASS"}
                    changed_after_clean = any(step["incremental_vs_clean"].values())
                else:
                    step["skipped_by_sut"] = sorted(set(results[0].get("rebuilt_keys", [])) & set(results[1].get("retained", [])))
                    step["recompilation"] = {v.name: {"rebuilt": len(r.get("rebuilt", [])),
                        "unchanged": r.get("unchanged_rebuilt")} for v, r in zip(variants, results)}
            # 清理前先留证；clean 恢复成功也不能抹掉已经观察到的异常。
            failed |= status != "PASS" or changed_after_clean
            if status != "PASS" or changed_after_clean or (last and previous is None):
                for variant, result in zip(variants, results):
                    variant.preserve(result)
                    write_json(folder / phase / variant.name / "result.json", result)
            row["steps"].append(step)
            write_json(destination / "summary.json", rows)
            print(f"{revision[:12]} {phase}: {status}", flush=True)
            if status == "INVALID" or (phase == "clean" and status != "PASS"):
                return 2 if status == "INVALID" else 1
            previous = results
    return int(failed)


def run(compilers_path: Path, projects_path: Path, destination: Path) -> int:
    compilers = Compilers.model_validate(yaml.safe_load(compilers_path.read_text())).compilers
    if set(compilers) != {"baseline", "sut"}:
        raise Invalid("compilers 必须同时提供 baseline 和 sut")
    projects = Projects.model_validate(yaml.safe_load(projects_path.read_text())).projects
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "config.json", dict(compilers={k: v.model_dump(mode="json") for k, v in compilers.items()},
                                               projects=[p.model_dump() for p in projects]))
    with ExitStack() as temporary:
        homes = {name: prepare_home(absolute(str(c.kotlin_native_home), compilers_path.parent), temporary)
                 for name, c in compilers.items()}
        if homes["baseline"] == homes["sut"] or any(
                a.is_relative_to(b) for a, b in [(homes["baseline"], homes["sut"]), (homes["sut"], homes["baseline"])]):
            raise Invalid("baseline 和 sut 必须使用不同的编译器目录")
        store = Store(destination / "store")
        if homes["baseline"].joinpath("klib/cache").resolve() == homes["sut"].joinpath("klib/cache").resolve():
            raise Invalid("baseline 和 sut 不能共用同一个编译缓存目录")
        write_json(destination / "toolchains.json", {name: {
            "home": str(home), "version": subprocess.check_output([str(home / "bin/konanc"), "-version"],
                stderr=subprocess.STDOUT, text=True),
            "compiler_jars": {str(p.relative_to(home)): store.record(p) for p in sorted((home / "konan/lib").glob("*.jar"))}
        } for name, home in homes.items()})
        for index, project in enumerate(projects):
            root = destination / f"project-{index}"
            root.mkdir()
            repo = project.repository
            if (projects_path.parent / repo).exists():
                repo = str((projects_path.parent / repo).resolve())
            subprocess.run(["git", "clone", repo, str(root / "baseline")], check=True)
            revisions = commits(root / "baseline", project)
            git(root / "baseline", "worktree", "add", "--detach", str(root / "sut"), revisions[0])
            variants = [Variant(name, root / name, homes[name], compilers[name], store) for name in ("baseline", "sut")]
            write_json(root / "commits.json", revisions)
            status = replay(project, variants, revisions, root / "results", store)
            if status:
                return status
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compilers", required=True, type=Path)
    parser.add_argument("--projects", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path, help="保存结果的目录，必须尚未创建")
    args = parser.parse_args()
    if args.run_dir.exists():
        parser.error("run-dir 已存在，请使用新目录")
    try:
        return run(args.compilers.resolve(), args.projects.resolve(), args.run_dir.resolve())
    except (Invalid, ValueError, OSError, yaml.YAMLError, tarfile.TarError, subprocess.CalledProcessError) as error:
        write_json(args.run_dir / "error.json", dict(status="INVALID", error=str(error)))
        print(f"INVALID: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
