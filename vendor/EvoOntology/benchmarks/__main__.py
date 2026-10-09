"""``python -m benchmarks`` — inspect the registered benchmark environments."""

from __future__ import annotations

import argparse
import sys

from .registry import adapter_class, describe, get, list_adapters


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description="Inspect the benchmark environments registered for the "
        "EvoOntology evolution loop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List registered benchmark environments")
    resolve = sub.add_parser(
        "resolve", help="Resolve a benchmark adapter class"
    )
    resolve.add_argument("name", help="Benchmark environment name")
    args = parser.parse_args(argv)

    if args.command == "list":
        for name in list_adapters():
            print(f"{name}\t{describe(name)}")
        return 0

    if args.command == "resolve":
        cls = adapter_class(args.name)
        print(
            f"{args.name}\t{describe(args.name)}\t"
            f"{cls.__module__}.{cls.__name__}"
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
