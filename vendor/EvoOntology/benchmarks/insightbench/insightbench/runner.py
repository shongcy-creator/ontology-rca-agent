"""Implementation for the insightbench.insightbench.runner module."""

import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import numpy as np
from tqdm import tqdm

from insightbench import agents as agent_mod
from insightbench import benchmarks
from insightbench.utils.exp_utils import save_json


def _normalize_domain(value: object) -> str:
    """Return a stable semantic-domain identifier."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "unknown").lower())
    return normalized.strip("_") or "unknown"


@dataclass
class FlagResult:
    """Implementation of FlagResult."""

    flag_id: int
    pred_insights: Optional[List[str]] = None
    pred_summary: Optional[str] = None
    score_insights: Optional[float] = None
    score_summary: Optional[float] = None
    error: Optional[str] = None
    success: bool = False
    generation_success: bool = False
    evaluation_success: bool = False
    evaluation_error: Optional[str] = None
    semantic_trace: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "flag_id": self.flag_id,
            "pred_insights": self.pred_insights,
            "pred_summary": self.pred_summary,
            "score_insights": self.score_insights,
            "score_summary": self.score_summary,
            "error": self.error,
            "success": self.success,
            "generation_success": self.generation_success,
            "evaluation_success": self.evaluation_success,
            "evaluation_error": self.evaluation_error,
            "semantic_trace": self.semantic_trace,
        }


@dataclass
class BatchResult:
    """Implementation of BatchResult."""

    flag_results: List[FlagResult]
    run_name: str
    mean_insights_per_flag: float = 0.0
    mean_summary_per_flag: float = 0.0
    success_count: int = 0
    fail_count: int = 0
    generation_count: int = 0
    evaluation_count: int = 0

    def to_dict(self) -> dict:
        return {
            "run_name": self.run_name,
            "mean_insights_per_flag": self.mean_insights_per_flag,
            "mean_summary_per_flag": self.mean_summary_per_flag,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "generation_count": self.generation_count,
            "evaluation_count": self.evaluation_count,
            "success_rate": (
                self.success_count / len(self.flag_results)
                if self.flag_results else 0.0
            ),
            "n_total": len(self.flag_results),
            "flag_results": [r.to_dict() for r in self.flag_results],
        }


class BatchRunner:
    """Implementation of BatchRunner."""

    def __init__(
        self,
        agent_config: dict,
        datadir: str = "data/notebooks",
        score_name: str = "g_eval",
        max_workers: int = 4,
        output_dir: Optional[str] = None,
    ):
        """Initialize the object."""
        self.agent_config = agent_config


        self._project_root = os.path.abspath(".")
        self.datadir = os.path.abspath(datadir)
        self.score_name = score_name
        self.max_workers = max_workers

        if output_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join("results", f"run_{timestamp}")
        self.output_dir = os.path.abspath(output_dir)

    def run_single_flag(self, flag_id: int) -> FlagResult:
        """Run single flag."""

        agent = None
        flag_result_path = os.path.join(
            self.output_dir, "flags", f"flag_{flag_id}.json"
        )


        if os.path.exists(flag_result_path):
            try:
                cached = benchmarks.load_dataset_dict(flag_result_path)
            except Exception:
                cached = {}

            if cached.get("success"):
                return FlagResult(**{k: v for k, v in cached.items()
                                     if k in FlagResult.__dataclass_fields__})

            if cached.get("generation_success") and cached.get("pred_insights"):
                return self._evaluate_only(flag_id, cached, flag_result_path)

        try:

            json_path = os.path.join(self.datadir, f"flag-{flag_id}.json")
            if not os.path.exists(json_path):
                result = FlagResult(
                    flag_id=flag_id,
                    error=f"JSON file does not exist: {json_path}",
                )
                save_json(flag_result_path, result.to_dict())
                return result

            dataset_dict = benchmarks.load_dataset_dict(json_path)

            for key in ("dataset_csv_path", "user_dataset_csv_path"):
                rel = dataset_dict.get(key)
                if rel:
                    dataset_dict[key] = os.path.normpath(
                        os.path.join(self._project_root, rel)
                    )
            metadata = dataset_dict.get("metadata") or {}
            goal = metadata.get("goal") or "I want to find interesting trends in this dataset"
            semantic_domain = _normalize_domain(metadata.get("category", "unknown"))
            context_parts = [
                metadata.get("dataset_description"),
                f"Role: {metadata['role']}" if metadata.get("role") else None,
                f"Category: {metadata['category']}" if metadata.get("category") else None,
            ]
            context = "\n".join(part for part in context_parts if part) or (
                "This is a dataset that could potentially consist of interesting insights"
            )


            flag_savedir = os.path.join(self.output_dir, "flags", f"flag_{flag_id}")
            agent = agent_mod.Agent(
                model_name=self.agent_config.get("model_name", "gpt-4o"),
                max_questions=self.agent_config.get("max_questions", 3),
                branch_depth=self.agent_config.get("branch_depth", 4),
                n_retries=self.agent_config.get("n_retries", 5),
                temperature=self.agent_config.get("temperature", 0),
                savedir=flag_savedir,
                goal=goal,
                context=context,
                semantic_enabled=self.agent_config.get("semantic_enabled", False),
                semantic_store_path=self.agent_config.get("semantic_store_path"),
                semantic_version=self.agent_config.get("semantic_version"),
                semantic_max_tool_rounds=self.agent_config.get(
                    "semantic_max_tool_rounds", 12
                ),
                semantic_domain=semantic_domain,
            )

            pred_insights, pred_summary = agent.get_insights(
                dataset_csv_path=dataset_dict.get("dataset_csv_path"),
                user_dataset_csv_path=dataset_dict.get("user_dataset_csv_path"),
                return_summary=True,
            )

            semantic_trace = agent.get_semantic_trace()
            result = FlagResult(
                flag_id=flag_id,
                pred_insights=pred_insights,
                pred_summary=pred_summary,
                generation_success=True,
                semantic_trace=semantic_trace,
            )

            save_json(flag_result_path, result.to_dict())


            gt_insights = dataset_dict.get("insights", [])
            gt_summary = dataset_dict.get("summary", "")
            try:
                score_insights = benchmarks.evaluate_insights(
                    pred_insights=pred_insights,
                    gt_insights=gt_insights,
                    score_name=self.score_name,
                )
                score_summary = benchmarks.evaluate_summary(
                    pred=pred_summary,
                    gt=gt_summary,
                    score_name=self.score_name,
                )
                result.score_insights = float(score_insights)
                result.score_summary = float(score_summary)
                result.evaluation_success = True
                result.success = True
                if semantic_trace is not None:
                    semantic_trace["outcome"] = {
                        "success": True,
                        "score_insights": result.score_insights,
                        "score_summary": result.score_summary,
                    }
            except Exception as eval_error:
                result.evaluation_error = (
                    f"{type(eval_error).__name__}: {eval_error}\n"
                    f"{traceback.format_exc()}"
                )
                if semantic_trace is not None:
                    semantic_trace["outcome"] = {
                        "success": False,
                        "stage": "evaluation",
                        "error_type": type(eval_error).__name__,
                    }

        except Exception as e:
            semantic_trace = agent.get_semantic_trace() if agent is not None else None
            if semantic_trace is not None:
                semantic_trace["outcome"] = {
                    "success": False,
                    "error_type": type(e).__name__,
                }
            result = FlagResult(
                flag_id=flag_id,
                error=f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}",
                semantic_trace=semantic_trace,
            )


        save_json(flag_result_path, result.to_dict())

        if (
            self.agent_config.get("record_trajectories")
            and self.agent_config.get("semantic_enabled")
            and semantic_trace is not None
        ):
            from evoontology import TrajectoryStore

            store_path = self.agent_config.get("semantic_store_path")
            if store_path:
                semantic_calls = [
                    {
                        "tool": event.get("type", ""),
                        "input": event.get("arguments", {}),
                        "result": {
                            "result_ids": event.get("result_ids", []),
                            "status": event.get("status", ""),
                        },
                    }
                    for event in semantic_trace.get("semantic_events", [])
                ]
                TrajectoryStore(store_path).append({
                    "task_id": f"flag_{flag_id}",
                    "question": goal,
                    "ontology_version": semantic_trace.get("semantic_version", "unknown"),
                    "split": self.agent_config.get("split", ""),
                    "semantic_calls": semantic_calls,
                    "native_tool_calls": [],
                    "final_answer": {
                        "insights": result.pred_insights or [],
                        "summary": result.pred_summary or "",
                    },
                    "task_status": "completed" if result.generation_success else "failed",
                    "errors": [result.error] if result.error else [],
                })

        return result

    def _evaluate_only(self, flag_id: int, cached: dict, result_path: str) -> FlagResult:
        """Evaluate only."""
        json_path = os.path.join(self.datadir, f"flag-{flag_id}.json")
        try:
            dataset_dict = benchmarks.load_dataset_dict(json_path)
        except Exception as e:
            return FlagResult(
                flag_id=flag_id,
                error=f"Cannot load flag JSON for eval-only: {e}",
            )

        pred_insights = cached.get("pred_insights", [])
        pred_summary = cached.get("pred_summary", "")
        gt_insights = dataset_dict.get("insights", [])
        gt_summary = dataset_dict.get("summary", "")

        result = FlagResult(
            flag_id=flag_id,
            pred_insights=pred_insights,
            pred_summary=pred_summary,
            generation_success=True,
            semantic_trace=cached.get("semantic_trace"),
        )

        try:
            score_insights = benchmarks.evaluate_insights(
                pred_insights=pred_insights,
                gt_insights=gt_insights,
                score_name=self.score_name,
            )
            score_summary = benchmarks.evaluate_summary(
                pred=pred_summary,
                gt=gt_summary,
                score_name=self.score_name,
            )
            result.score_insights = float(score_insights)
            result.score_summary = float(score_summary)
            result.evaluation_success = True
            result.success = True
        except Exception as eval_error:
            result.evaluation_error = (
                f"{type(eval_error).__name__}: {eval_error}\n{traceback.format_exc()}"
            )

        save_json(result_path, result.to_dict())
        return result

    def run_batch(
        self,
        flag_ids: List[int],
        run_name: str = "evaluation",
        show_progress: bool = True,
    ) -> BatchResult:
        """Run batch."""
        os.makedirs(os.path.join(self.output_dir, "flags"), exist_ok=True)

        flag_results: List[FlagResult] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.run_single_flag, fid): fid for fid in flag_ids
            }

            if show_progress:
                pbar = tqdm(
                    total=len(flag_ids),
                    desc=f"Running {run_name}",
                    unit="flag",
                )

            for future in as_completed(futures):
                flag_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = FlagResult(
                        flag_id=flag_id,
                        error=f"Worker exception: {type(e).__name__}: {str(e)}",
                    )
                flag_results.append(result)

                if show_progress:
                    success_count = sum(1 for r in flag_results if r.success)
                    pbar.set_postfix(
                        {"success": success_count, "fail": len(flag_results) - success_count}
                    )
                    pbar.update(1)

            if show_progress:
                pbar.close()


        flag_results.sort(key=lambda r: r.flag_id)


        successful = [r for r in flag_results if r.success]
        failed = [r for r in flag_results if not r.success]
        generated = [r for r in flag_results if r.generation_success]
        evaluated = [r for r in flag_results if r.evaluation_success]


        mean_insights_per_flag = float(np.mean([
            r.score_insights if r.success and r.score_insights is not None else 0.0
            for r in flag_results
        ])) if flag_results else 0.0
        mean_summary_per_flag = float(np.mean([
            r.score_summary if r.success and r.score_summary is not None else 0.0
            for r in flag_results
        ])) if flag_results else 0.0
        batch_result = BatchResult(
            flag_results=flag_results,
            run_name=run_name,
            mean_insights_per_flag=mean_insights_per_flag,
            mean_summary_per_flag=mean_summary_per_flag,
            success_count=len(successful),
            fail_count=len(failed),
            generation_count=len(generated),
            evaluation_count=len(evaluated),
        )


        save_json(
            os.path.join(self.output_dir, "results.json"), batch_result.to_dict()
        )


        print(f"\n{'='*60}")
        print(f"Batch complete: {run_name}")
        print(f"  Success: {batch_result.success_count}, failed: {batch_result.fail_count}")
        print(
            f"  Predictions generated: {batch_result.generation_count}, "
            f"Evaluations completed: {batch_result.evaluation_count}"
        )
        print(f"  Mean insights score:  {mean_insights_per_flag:.4f}")
        print(f"  Mean summary score:   {mean_summary_per_flag:.4f}")
        print(f"  Results directory: {self.output_dir}")
        print(f"{'='*60}")

        return batch_result
