"""Command-line entry point for running InsightBench evaluations."""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from insightbench import runner as runner_mod

load_dotenv()
os.environ.setdefault("MPLBACKEND", "Agg")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


_FLAG_FILE = re.compile(r"^flag-(\d+)\.json$")


def discover_flag_ids(datadir: str) -> list[int]:
    """Return sorted flag IDs discovered in ``datadir``."""
    ids = []
    for path in Path(datadir).glob("flag-*.json"):
        match = _FLAG_FILE.match(path.name)
        if match:
            ids.append(int(match.group(1)))
    return sorted(set(ids))


def parse_flag_ids(value: str) -> list[int]:
    """Parse a comma-separated list of integer flag IDs."""
    if not value.strip():
        return []
    try:
        return sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--flag-ids must be a comma-separated list of integers"
        ) from exc


def configure_environment(args: argparse.Namespace) -> None:
    """Apply optional model endpoint and credential overrides."""
    overrides = {
        "OPENAI_API_KEY": args.openai_api_key,
        "AGENT_API_KEY": args.agent_api_key,
        "AGENT_BASE_URL": args.agent_base_url,
        "OPENAI_BASE_URL": args.base_url,
        "EVAL_API_KEY": args.eval_api_key,
        "EVAL_BASE_URL": args.eval_base_url,
        "EVAL_MODEL_NAME": args.eval_model or args.model,
    }
    for name, value in overrides.items():
        if value:
            os.environ[name] = value


def cmd_run(args: argparse.Namespace) -> None:
    """Run the agent and evaluator for selected or discovered flags."""
    flag_ids = args.flag_ids or discover_flag_ids(args.datadir)
    if not flag_ids:
        raise SystemExit(
            f"No flag JSON files found in {args.datadir!r}; "
            "provide data or pass --flag-ids."
        )

    configure_environment(args)
    agent_config = {
        "model_name": args.model,
        "max_questions": args.max_questions,
        "branch_depth": args.branch_depth,
        "n_retries": args.n_retries,
        "temperature": 0,
        "semantic_enabled": args.ontology_layer,
        "semantic_store_path": args.semantic_store,
        "semantic_version": args.semantic_version,
        "semantic_max_tool_rounds": args.semantic_max_tool_rounds,
        "record_trajectories": args.record_trajectories,
        "split": args.split,
        "base_url": args.agent_base_url
        or args.base_url
        or os.getenv("OPENAI_BASE_URL"),
    }

    output_dir = args.output_dir
    if output_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join("results", f"run_{timestamp}")

    print(f"Running {len(flag_ids)} flags")
    runner = runner_mod.BatchRunner(
        agent_config=agent_config,
        datadir=args.datadir,
        score_name=args.score,
        max_workers=args.max_workers,
        output_dir=output_dir,
    )
    runner.run_batch(flag_ids=flag_ids, run_name="selected")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="InsightBench ontology-layer agent evaluation"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run agent generation and evaluation")
    run.add_argument(
        "-d", "--datadir", default="data/notebooks",
        help="Directory containing flag JSON files",
    )
    run.add_argument(
        "--flag-ids", type=parse_flag_ids, default=[],
        help="Optional comma-separated flag IDs; default: discover all flag JSON files",
    )
    run.add_argument("--model", default=os.getenv("MODEL", "gpt-4o"))
    run.add_argument("--max-questions", type=int, default=3)
    run.add_argument("--branch-depth", type=int, default=4)
    run.add_argument("--n-retries", type=int, default=5)
    run.add_argument(
        "--score", default="g_eval",
        choices=["g_eval", "rouge1", "llama3_eval"],
    )
    run.add_argument("--max-workers", type=int, default=40)
    run.add_argument("--output-dir", default=None)
    run.add_argument("-o", "--openai-api-key", default=None)
    run.add_argument(
        "--ontology-layer",
        "--semantic-layer",
        dest="ontology_layer",
        action="store_true",
        help="Enable the ontology layer (--semantic-layer is a compatibility alias)",
    )
    run.add_argument("--semantic-store", default=None)
    run.add_argument(
        "--semantic-version", default=None,
        help="Explicit semantic version to load (default: active)",
    )
    run.add_argument("--semantic-max-tool-rounds", type=int, default=12)
    run.add_argument(
        "--record-trajectories", action="store_true",
        help="Write normalized trajectories to the semantic workspace",
    )
    run.add_argument(
        "--split", default="",
        help="Split label recorded in the trajectory (e.g. train/test)",
    )
    run.add_argument("--base-url", default=None)
    run.add_argument("--agent-api-key", default=None)
    run.add_argument("--agent-base-url", default=None)
    run.add_argument("--eval-api-key", default=None)
    run.add_argument("--eval-base-url", default=None)
    run.add_argument("--eval-model", default=None)
    return parser


if __name__ == "__main__":
    cli_args = build_parser().parse_args()
    if cli_args.command == "run":
        cmd_run(cli_args)
