#!/usr/bin/env python3
"""Sync the root ``evoontology/`` core into the plugin bundles.

The root ``evoontology/`` package is the single source of truth. The Claude
Code and Codex plugins ship private copies so they work standalone; this
script mirrors the root package into both plugin copies, removes files that
no longer exist in the source (including stray ``__pycache__``), and reports
what changed.

Usage::

    python scripts/sync_plugin_core.py [--check]

``--check`` exits non-zero when the copies are out of sync instead of
writing.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "evoontology"
TARGETS = [
    REPO_ROOT / "plugins" / "claude-code" / "evoontology",
    REPO_ROOT / "plugins" / "evoontology-codex" / "evoontology",
]
IGNORE_DIRS = {"__pycache__"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files() -> dict[str, str]:
    """Map of relative POSIX path -> sha256 for every source file."""
    files: dict[str, str] = {}
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(SOURCE)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        files[rel.as_posix()] = _sha256(path)
    return files


def target_files(target: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not target.exists():
        return files
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(target)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        files[rel.as_posix()] = _sha256(path)
    return files


def diff(source: dict[str, str], current: dict[str, str]):
    added = sorted(k for k in source if k not in current)
    updated = sorted(k for k in source if k in current and source[k] != current[k])
    removed = sorted(k for k in current if k not in source)
    return added, updated, removed


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _remove_tree(child)
        path.rmdir()
    else:
        path.unlink()


def sync_target(target: Path, source: dict[str, str], check_only: bool) -> list[str]:
    """Mirror ``source`` into ``target``; return human-readable changes."""
    current = target_files(target)
    added, updated, removed = diff(source, current)
    changes = [f"+ {name}" for name in added]
    changes += [f"~ {name}" for name in updated]
    changes += [f"- {name}" for name in removed]

    if check_only:
        return changes

    for name in added + updated:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((SOURCE / name).read_bytes())
    for name in removed:
        _remove_tree(target / name)
    for stale in target.rglob("__pycache__"):
        _remove_tree(stale)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args(argv)

    if not SOURCE.is_dir():
        print(f"source package not found: {SOURCE}", file=sys.stderr)
        return 1

    source = source_files()
    exit_code = 0
    for target in TARGETS:
        changes = sync_target(target, source, args.check)
        rel_target = target.relative_to(REPO_ROOT)
        if not changes:
            print(f"{rel_target}: up to date ({len(source)} files)")
            continue
        verb = "out of sync" if args.check else "updated"
        print(f"{rel_target}: {verb} ({len(changes)} change(s))")
        for change in changes:
            print(f"  {change}")
        if args.check:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
