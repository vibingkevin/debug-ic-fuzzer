#!/usr/bin/env python3
"""Wait for every check on a pull request to finish."""

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pull_request", help="Pull request number or URL")
    parser.add_argument("--repo", default="vibingkevin/debug-ic-fuzzer")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    command = [
        "gh",
        "pr",
        "checks",
        args.pull_request,
        "--repo",
        args.repo,
        "--watch",
        "--interval",
        str(args.interval),
        "--fail-fast",
    ]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    sys.exit(main())
