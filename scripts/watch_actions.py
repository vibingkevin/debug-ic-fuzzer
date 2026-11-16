#!/usr/bin/env python3
"""Watch the latest main-branch run of every active GitHub Actions workflow."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode


ACCEPTED = {"success", "skipped", "neutral"}


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, errors="replace", timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh exited {result.returncode}")
    return result.stdout


def pages(endpoint, key):
    return [item for page in json.loads(gh("api", "--paginate", "--slurp", endpoint)) for item in page[key]]


def latest_runs(repo, branch):
    workflows = {workflow["id"] for workflow in pages(f"repos/{repo}/actions/workflows?per_page=100", "workflows") if workflow["state"] == "active"}
    latest = {}
    page = 1
    while workflows - latest.keys():
        query = urlencode(dict(branch=branch, per_page=100, page=page))
        runs = json.loads(gh("api", f"repos/{repo}/actions/runs?{query}"))["workflow_runs"]
        for run in runs:
            if run["workflow_id"] in workflows:
                latest.setdefault(run["workflow_id"], run)
        if len(runs) < 100:
            break
        page += 1
    return sorted(latest.values(), key=lambda run: run["name"])


def failed(item):
    return item["status"] == "completed" and item["conclusion"] not in ACCEPTED


def poll(repo, branch, output, previous, reported):
    runs = latest_runs(repo, branch)
    if not runs:
        raise RuntimeError(f"No workflow runs found for {repo} on {branch}")
    failures = False
    for run in runs:
        jobs = pages(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100", "jobs")
        run["jobs"] = jobs
        failures |= failed(run) or any(failed(job) for job in jobs)
        state = (run["id"], run["run_attempt"], run["status"], run["conclusion"], tuple((job["id"], job["status"], job["conclusion"]) for job in jobs))
        if previous.get(run["workflow_id"]) != state:
            counts = Counter(job["conclusion"] or job["status"] for job in jobs)
            progress = ", ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "no jobs yet"
            print(f"{run['name']}: {run['conclusion'] or run['status']} ({progress}) {run['html_url']}", flush=True)
            previous[run["workflow_id"]] = state
        for job in jobs:
            key = (run["id"], run["run_attempt"], job["id"])
            if not failed(job) or key in reported:
                continue
            steps = ", ".join(step["name"] for step in job.get("steps", []) if step["conclusion"] not in ACCEPTED | {None})
            print(f"FAILED: {job['name']} ({job['conclusion']}) {steps}\n{job['html_url']}", flush=True)
            try:
                log = gh("api", f"repos/{repo}/actions/jobs/{job['id']}/logs")
            except RuntimeError as error:
                print(f"Job log unavailable; will retry: {error}", file=sys.stderr, flush=True)
                continue
            path = output / f"{run['id']}-attempt-{run['run_attempt']}-job-{job['id']}.log"
            path.write_text(log)
            print(f"Saved failure log: {path}\n" + "\n".join(log.splitlines()[-20:]), flush=True)
            reported.add(key)
    snapshot = dict(checked_at=datetime.now(timezone.utc).isoformat(), repository=repo, branch=branch, runs=runs)
    temporary = output / "latest.json.tmp"
    temporary.write_text(json.dumps(snapshot, indent=2) + "\n")
    temporary.replace(output / "latest.json")
    return all(run["status"] == "completed" for run in runs), failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="vibingkevin/debug-ic-fuzzer")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")
    parser.add_argument("--output-dir", type=Path, default=Path(".actions-monitor"))
    parser.add_argument("--once", action="store_true", help="Check once instead of waiting for completion")
    parser.add_argument("--watch", action="store_true", help="Keep watching for new runs after completion")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    previous, reported = {}, set()
    while True:
        try:
            complete, failures = poll(args.repo, args.branch, output, previous, reported)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            print(f"Monitor error: {error}", file=sys.stderr, flush=True)
            if args.once or not previous:
                return 2
        else:
            if args.once or (complete and not args.watch):
                return int(failures)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
