#!/usr/bin/env python3
"""Evolution adapter for InsightBench: evaluate one semantic version.

Runs ``main.py run --ontology-layer`` as a subprocess with
``--semantic-version`` and reduces ``results.json`` into the adapter result
contract::

    {"metrics": {...}, "cases": [...], "artifact_paths": [...]}

Stdlib-only so the evolution core can import it without InsightBench
dependencies.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

IB_DIR = Path(__file__).resolve().parent


class InsightBenchEvolutionAdapter:
    """Evaluate a semantic version on the InsightBench flag tasks."""

    def __init__(
        self,
        store_path: str = "",
        datadir: str = "",
        output_dir: str = "",
        python: str = "",
        extra_args: Optional[List[str]] = None,
    ):
        self.store_path = store_path or str(IB_DIR / ".evoontology")
        self.datadir = datadir
        self.output_dir = output_dir
        self.python = python or sys.executable
        self.extra_args = list(extra_args or [])

    def evaluate(
        self,
        subject: str,
        cases: Optional[List[str]] = None,
        output_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the flag batch for ``subject`` and summarize ``results.json``."""
        output_dir = str(
            output_hint or self.output_dir
            or IB_DIR / "results" / "evolution" / str(subject)
        )
        cmd = [
            self.python, str(IB_DIR / "main.py"), "run",
            "--ontology-layer",
            "--semantic-store", self.store_path,
            "--semantic-version", str(subject),
            "--output-dir", output_dir,
        ]
        if self.datadir:
            cmd += ["--datadir", self.datadir]
        if cases:
            cmd += ["--flag-ids", ",".join(str(c) for c in cases)]
        cmd += self.extra_args

        completed = subprocess.run(cmd, cwd=str(IB_DIR), capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"InsightBench run failed for {subject!r} "
                f"(exit {completed.returncode}): {completed.stderr[-500:]}"
            )

        result_file = Path(output_dir) / "results.json"
        if not result_file.is_file():
            raise FileNotFoundError(f"Missing results.json at {result_file}")
        data = json.loads(result_file.read_text(encoding="utf-8"))

        case_results = []
        for flag in data.get("flag_results", []):
            if flag.get("error") or not flag.get("generation_success"):
                status = "error"
            elif flag.get("success"):
                status = "correct"
            else:
                status = "incorrect"
            case_results.append({
                "id": flag.get("flag_id"),
                "score": float(flag.get("score_insights") or 0.0),
                "status": status,
            })
        return {
            "metrics": {
                "mean_insights_per_flag": data.get("mean_insights_per_flag", 0.0),
                "mean_summary_per_flag": data.get("mean_summary_per_flag", 0.0),
                "success_rate": data.get("success_rate", 0.0),
            },
            "cases": case_results,
            "artifact_paths": [str(result_file)],
        }
