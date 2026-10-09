#!/usr/bin/env python3
"""Run comparable BIRD baseline and semantic evaluations."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=["minidev", "dev"], default="minidev")
    parser.add_argument("--split-dir", default="")
    parser.add_argument("--output", default="results/full")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--record-trajectories", action="store_true")
    args = parser.parse_args()

    for condition in ("baseline", "semantic"):
        command = [
            sys.executable,
            "run_evaluation.py",
            "--config", f"configs/{condition}.yaml",
            "--dataset", args.dataset,
            "--output", str(Path(args.output) / condition),
            "--parallel", str(args.parallel),
            "--save-traces",
        ]
        if args.split_dir:
            command.extend(["--split-dir", args.split_dir])
        if args.limit:
            command.extend(["--limit", str(args.limit)])
        if args.record_trajectories and condition == "semantic":
            command.append("--record-trajectories")
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
