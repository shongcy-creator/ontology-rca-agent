#!/usr/bin/env python3
"""Evolution adapter for DDR-10K: generate agent logs, then judge them.

One evaluation = ``run_agent.py`` (trajectory generation with the selected
semantic version) + ``run_evaluation.py`` (LLM judge). The judge JSON is
reduced into the adapter result contract::

    {"metrics": {...}, "cases": [...], "artifact_paths": [...]}

Stdlib-only so the evolution core can import it without DDR dependencies.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DDR_DIR = Path(__file__).resolve().parent


class DDREvolutionAdapter:
    """Evaluate a semantic version on one DDR scenario (mimic/10k/globem)."""

    def __init__(
        self,
        scenario: str,
        config_path: str = "",
        log_dir: str = "",
        python: str = "",
        skip_generation: bool = False,
        extra_args: Optional[List[str]] = None,
    ):
        self.scenario = scenario
        self.config_path = config_path
        self.log_dir = log_dir
        self.python = python or sys.executable
        self.skip_generation = skip_generation
        self.extra_args = list(extra_args or [])

    def evaluate(
        self,
        subject: str,
        cases: Optional[List[str]] = None,
        output_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate trajectories for ``subject`` (unless skipped), judge them."""
        work_dir = Path(output_hint or (DDR_DIR / "outputs" / "evolution" / str(subject)))
        work_dir.mkdir(parents=True, exist_ok=True)
        log_dir = Path(self.log_dir) if self.log_dir else work_dir / "logs"

        if not self.skip_generation:
            gen_cmd = [
                self.python, str(DDR_DIR / "run_agent.py"),
                "--scenario", self.scenario,
                "--log-dir", str(log_dir),
                "--semantic-version", str(subject),
                "--yes",
            ]
            if self.config_path:
                gen_cmd += ["--config", self.config_path]
            if cases:
                gen_cmd += ["--target-ids", ",".join(str(c) for c in cases)]
            gen_cmd += self.extra_args
            completed = subprocess.run(gen_cmd, cwd=str(DDR_DIR), capture_output=True, text=True)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"DDR generation failed for {subject!r} "
                    f"(exit {completed.returncode}): {completed.stderr[-500:]}"
                )

        result_file = work_dir / f"{self.scenario}_{subject}_evaluation_result.json"
        eval_cmd = [
            self.python, str(DDR_DIR / "run_evaluation.py"),
            "--scenario", self.scenario,
            "--log-dir", str(log_dir),
            "--output", str(result_file),
        ]
        if self.config_path:
            eval_cmd += ["--config", self.config_path]
        completed = subprocess.run(eval_cmd, cwd=str(DDR_DIR), capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"DDR evaluation failed for {subject!r} "
                f"(exit {completed.returncode}): {completed.stderr[-500:]}"
            )

        data = json.loads(result_file.read_text(encoding="utf-8"))
        case_results = []
        for entity in data.get("entity_results", []):
            summary = entity.get("summary", {})
            score = summary.get("message_wise_context", {}).get("correct_percentage")
            if score is None:
                score = summary.get("chat_wise_context", {}).get("correct_percentage")
            if entity.get("error"):
                status = "error"
            elif (score or 0) > 0:
                status = "correct"
            else:
                status = "incorrect"
            case_results.append({
                "id": entity.get("entity_id"),
                "score": float(score or 0) / 100.0,
                "status": status,
            })
        return {
            "metrics": data.get("overall_statistics", {}),
            "cases": case_results,
            "artifact_paths": [str(result_file)],
        }