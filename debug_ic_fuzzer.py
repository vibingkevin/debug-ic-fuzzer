"""命令行入口与 baseline / sut 逐提交回放。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import shutil
from contextlib import ExitStack, contextmanager
from pathlib import Path
import subprocess
import sys
import tarfile

import yaml

from impl.build import Variant
from impl.config import Compilers, Invalid, Project, absolute, default_home, prepare_home, expand_args
from impl.results import Store, differences, pair_status, write_json


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


REPOSITORIES = Path("/tmp/debug-ic-fuzzer")


@contextmanager
def checkouts(project: Project):
    name = project.repository.rsplit("/", 1)[-1].removesuffix(".git")
    key = hashlib.md5(project.repository.encode()).hexdigest()[:8]
    root = (REPOSITORIES / f"{name}-{key}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 不同配置也会复用同一仓库，避免同时切换源码或清理另一轮的缓存。
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Invalid(f"仓库正在被另一次运行使用：{root}") from None
        if (root / "project").exists():
            raise Invalid(f"编译目录尚未恢复，请先检查：{root / 'project'}")
        baseline, sut = root / "baseline", root / "sut"
        if not baseline.exists():
            subprocess.run(["git", "clone", project.repository, str(baseline)], check=True)
        if git(baseline, "config", "--get", "remote.origin.url") != project.repository:
            raise Invalid(f"已有仓库的 origin 与配置不一致：{baseline}")
        for checkout in [baseline, sut]:
            if not checkout.exists():
                continue
            common = absolute(git(checkout, "rev-parse", "--git-common-dir"), checkout)
            if common != (baseline / ".git").resolve():
                raise Invalid(f"目录不属于同一个仓库：{checkout}")
            if git(checkout, "status", "--porcelain"):
                raise Invalid(f"仓库有未提交修改：{checkout}")
        git(baseline, "fetch", "origin")
        git(baseline, "checkout", project.branch)
        git(baseline, "merge", "--ff-only", f"origin/{project.branch}")
        if not sut.exists():
            git(baseline, "worktree", "add", "--detach", str(sut), "HEAD")
        else:
            git(sut, "checkout", "--detach", f"origin/{project.branch}")
        yield baseline, sut


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
    project = Project.model_validate(yaml.safe_load(projects_path.read_text()))
    project.gradle_args = expand_args(project.gradle_args, projects_path.parent)
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "config.json", dict(compilers={k: v.model_dump(mode="json") for k, v in compilers.items()},
                                               project=project.model_dump()))
    with ExitStack() as temporary:
        homes = {name: (prepare_home(absolute(str(c.kotlin_native_home), compilers_path.parent), temporary)
                        if c.kotlin_native_home is not None else default_home(c.kotlin_version))
                 for name, c in compilers.items()}
        if homes["baseline"] == homes["sut"] or any(
                a.is_relative_to(b) for a, b in [(homes["baseline"], homes["sut"]), (homes["sut"], homes["baseline"])]):
            raise Invalid("baseline 和 sut 必须使用不同的编译器目录")
        store = Store(destination / "store")
        if homes["baseline"].joinpath("klib/cache").resolve() == homes["sut"].joinpath("klib/cache").resolve():
            raise Invalid("baseline 和 sut 不能共用同一个编译缓存目录")
        with checkouts(project) as (baseline, sut):
            # 新实验重置项目本地状态；同一回放内保留各版增量状态，全局下载缓存不动。
            for checkout in (baseline, sut):
                for name in (".gradle", ".kotlin"):
                    cache = checkout / name
                    if cache.is_symlink():
                        raise Invalid(f"项目状态目录不能共享到外部：{cache}")
                    if cache.is_dir():
                        shutil.rmtree(cache)
            revisions = commits(baseline, project)
            variants = [Variant(name, checkout, homes[name], compilers[name], store)
                        for name, checkout in [("baseline", baseline), ("sut", sut)]]
            write_json(destination / "commits.json", revisions)
            write_json(destination / "checkouts.json", dict(baseline=str(baseline), sut=str(sut),
                                                   build=str(baseline.parent / "project")))
            status = replay(project, variants, revisions, destination / "results", store)
        # 默认编译器可能直到首次构建才由 KGP 下载，失败时也记录已知路径。
        write_json(destination / "toolchains.json", {name: {
            "home": str(home), "version": subprocess.check_output([str(home / "bin/konanc"), "-version"],
                stderr=subprocess.STDOUT, text=True) if (home / "bin/konanc").is_file() else None,
            "compiler_jars": {str(p.relative_to(home)): store.record(p) for p in sorted((home / "konan/lib").glob("*.jar"))}
        } for name, home in homes.items()})
        if status:
            return status
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compilers", required=True, type=Path)
    parser.add_argument("--projects", required=True, type=Path)
    args = parser.parse_args()
    destination = (Path("runs") / f"{args.compilers.stem}-{args.projects.stem}").resolve()
    if destination.exists():
        parser.error(f"运行目录已存在，请先移走或删除：{destination}")
    try:
        return run(args.compilers.resolve(), args.projects.resolve(), destination)
    except (Invalid, ValueError, OSError, yaml.YAMLError, tarfile.TarError, subprocess.CalledProcessError) as error:
        write_json(destination / "error.json", dict(status="INVALID", error=str(error)))
        print(f"INVALID: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
