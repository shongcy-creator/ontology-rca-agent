#!/usr/bin/env python3
"""Evolution adapter for BIRD: evaluate one semantic version end-to-end.

Runs ``run_evaluation.py`` as a subprocess with ``--semantic-version`` and
reduces the newest ``all_results.json`` into the adapter result contract::

    {"metrics": {...}, "cases": [...], "artifact_paths": [...]}

Stdlib-only so the evolution core can import it without BIRD dependencies.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BIRD_DIR = Path(__file__).resolve().parent


class BirdEvolutionAdapter:
    """Evaluate a semantic version against a BIRD split."""

    def __init__(
        self,
        config_path: str,
        dataset: str = "minidev",
        split_dir: str = "",
        db_ids: str = "",
        output_dir: str = "",
        python: str = "",
        extra_args: Optional[List[str]] = None,
    ):
        self.config_path = str(config_path)
        self.dataset = dataset
        self.split_dir = split_dir
        self.db_ids = db_ids
        self.output_dir = output_dir or str(BIRD_DIR / "results" / "evolution")
        self.python = python or sys.executable
        self.extra_args = list(extra_args or [])

    def evaluate(
        self,
        subject: str,
        cases: Optional[List[str]] = None,
        output_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the BIRD evaluation for ``subject`` and summarize it."""
        output_dir = str(output_hint or self.output_dir)
        cmd = [
            self.python, str(BIRD_DIR / "run_evaluation.py"),
            "--config", self.config_path,
            "--dataset", self.dataset,
            "--semantic-version", str(subject),
            "--output", output_dir,
        ]
        if self.split_dir:
            cmd += ["--split-dir", self.split_dir]
        db_ids = ",".join(str(c) for c in cases) if cases else self.db_ids
        if db_ids:
            cmd += ["--db-ids", db_ids]
        cmd += self.extra_args

        completed = subprocess.run(cmd, cwd=str(BIRD_DIR), capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"BIRD evaluation failed for {subject!r} "
                f"(exit {completed.returncode}): {completed.stderr[-500:]}"
            )

        result_file = self._latest_results(Path(output_dir))
        data = json.loads(result_file.read_text(encoding="utf-8"))
        case_results = []
        for entry in data.get("results", []):
            if entry.get("error"):
                status = "error"
            elif entry.get("ex"):
                status = "correct"
            else:
                status = "incorrect"
            case_results.append({
                "id": entry.get("question_id"),
                "score": 1.0 if entry.get("ex") else 0.0,
                "status": status,
            })
        return {
            "metrics": data.get("metrics", {}),
            "cases": case_results,
            "artifact_paths": [str(result_file)],
        }

    @staticmethod
    def _latest_results(output_dir: Path) -> Path:
        candidates = sorted(
            output_dir.rglob("all_results.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            raise FileNotFoundError(f"No all_results.json found under {output_dir}")
        return candidates[-1]