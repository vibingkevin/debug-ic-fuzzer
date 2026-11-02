"""Build each selected commit with baseline and sut."""
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import argparse
import json
import subprocess
import tarfile

import yaml

from impl.build import build
from impl.collection import collect
from impl.comparison import collect_products, compare, save_artifacts
from impl.config import Compilers, Project, load, prepare_home
from impl.repository import prepare_repository, create_worktree, checkout_worktree


def run(compilers_path: Path, project_path: Path) -> int:
    compilers = load(compilers_path, Compilers).compilers
    project = load(project_path, Project)
    root = Path.cwd()
    destination = root / 'run' / f'{compilers_path.stem}-{project_path.stem}-{datetime.now():%Y%m%d-%H%M%S}'
    destination.mkdir(parents=True)
    with ExitStack() as temporary:
        homes = {name: prepare_home(c, compilers_path.parent, temporary) for name, c in compilers.items()}
        if (homes['baseline'] == homes['sut'] or
                (homes['baseline'] / 'klib/cache').resolve() == (homes['sut'] / 'klib/cache').resolve()):
            raise ValueError('baseline and sut cannot use the same Native home or cache')
        repository, revisions = prepare_repository(root / 'repos', project)
        for name in ('baseline', 'sut'):
            create_worktree(repository, destination / name, revisions[0])
        failed = False
        last_links = {'baseline': None, 'sut': None}
        for index, revision in enumerate(revisions):
            results = {}
            for name in ('baseline', 'sut'):
                record_name = f'{index:04d}-{revision[:12]}-{name}'
                with checkout_worktree(repository, destination / name, revision) as project_directory:
                    result = build(project_directory, destination, name, revision, project, compilers[name],
                                   homes[name], project_path.parent, record_name, last_links[name])
                    try:
                        collect(result, destination)
                        if result['exit_code'] == 0 and result['error'] is None:
                            collect_products(result, destination)
                    except (ValueError, OSError, subprocess.CalledProcessError) as error:
                        result['error'] = result['error'] or str(error)
                    results[name] = result
                    (destination / f'{record_name}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
                failed |= result['exit_code'] != 0 or result['error'] is not None
                last_links[name] = result['link']
                print(f"{index:04d} {revision[:12]} {name}: exit={result['exit_code']} error={result['error']}", flush=True)
            comparison = {"error": "Build or collection failed"}
            try:
                if failed:
                    save_artifacts(results, destination)
                else:
                    comparison = compare(results, destination)
            except (ValueError, OSError) as error:
                comparison = {"error": str(error)}
            (destination / f'{index:04d}-{revision[:12]}-comparison.json').write_text(
                json.dumps(comparison, indent=2) + '\n')
            print(f"{index:04d} comparison: {comparison}", flush=True)
            failed |= comparison.get('production_equal') is not True
            if failed:
                break
        print(destination, flush=True)
        return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compilers', required=True, type=Path)
    parser.add_argument('--project', required=True, type=Path)
    args = parser.parse_args()
    try:
        return run(args.compilers.resolve(), args.project.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError, yaml.YAMLError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
