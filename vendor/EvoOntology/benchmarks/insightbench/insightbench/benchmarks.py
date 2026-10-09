"""Dataset loading and scoring helpers used by the batch runner."""

import json

from insightbench import metrics
from insightbench.utils.metrics_utils import score_insight


def load_dataset_dict(dataset_json_path: str) -> dict:
    """Load one InsightBench flag description."""
    with open(dataset_json_path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def evaluate_insights(pred_insights, gt_insights, score_name: str = "rouge1"):
    """Score predicted insights with the requested evaluator."""
    if score_name == "rouge1":
        score, _ = metrics.compute_rouge(pred_insights, gt_insights)
    elif score_name == "g_eval":
        score, _ = metrics.compute_g_eval_o2m(pred_insights, gt_insights)
    elif score_name == "llama3_eval":
        score, _ = metrics.compute_llama3_eval_o2m(pred_insights, gt_insights)
    else:
        raise ValueError(f"Unknown score_name: {score_name}")
    return score


def evaluate_summary(pred, gt, score_name: str = "rouge1"):
    """Score a generated summary."""
    return score_insight(pred_insight=pred, gt_insight=gt, score_name=score_name)
