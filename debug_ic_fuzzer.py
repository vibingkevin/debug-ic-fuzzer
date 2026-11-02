"""Build the first selected commit with baseline and sut."""
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import argparse
import json
import subprocess
import tarfile

import yaml

from impl.build import build
from impl.config import Compilers, Project, load, prepare_home
from impl.repository import prepare_repository, create_worktree


def run(compilers_path: Path, project_path: Path) -> int:
    compilers = load(compilers_path, Compilers).compilers
    project = load(project_path, Project)
    root = Path.cwd()
    destination = root / 'run' / f'{compilers_path.stem}-{project_path.stem}-{datetime.now():%Y%m%d-%H%M%S}'
    destination.mkdir(parents=True)
    with ExitStack() as temporary:
        homes = {name: prepare_home(c, compilers_path.parent, temporary) for name, c in compilers.items()}
        if homes['baseline'] == homes['sut']:
            raise ValueError('baseline and sut cannot use the same Native home')
        repository, revision = prepare_repository(root / 'repos', project)
        for name in ('baseline', 'sut'):
            create_worktree(repository, destination / name, revision)
        failed = False
        for name in ('baseline', 'sut'):
            result = build(repository, destination, name, revision, project, compilers[name], homes[name], project_path.parent)
            (destination / f'{name}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            failed |= result['exit_code'] != 0 or result['error'] is not None
            print(f"{name}: exit={result['exit_code']} error={result['error']}", flush=True)
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
