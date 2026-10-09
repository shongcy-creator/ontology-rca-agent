"""Evaluation gate tests: GT accept/reject, LLM-judge aggregation, anonymization, decode."""

import pytest

from evoontology import EvaluationGate

# ---- ground truth ----------------------------------------------------------

def test_decide_gt_accept():
    result = EvaluationGate.decide_gt([0.7, 0.8], [0.9, 0.9])
    assert result["accept"] is True
    assert result["protocol"] == "ground_truth"
    assert result["parent_score"] == pytest.approx(0.75)
    assert result["candidate_score"] == pytest.approx(0.9)
    assert result["delta"] == pytest.approx(0.15)


def test_decide_gt_reject():
    result = EvaluationGate.decide_gt([0.9, 0.9], [0.7, 0.8])
    assert result["accept"] is False
    assert result["delta"] < 0


def test_decide_gt_requires_scores():
    with pytest.raises(ValueError):
        EvaluationGate.decide_gt([], [0.5])


# ---- llm judge -------------------------------------------------------------

def test_decide_judge_accept():
    verdicts = [
        {"winner": "candidate", "critical_error": False},
        {"winner": "candidate", "critical_error": False},
        {"winner": "parent", "critical_error": False},
    ]
    result = EvaluationGate.decide_judge(verdicts)
    assert result["accept"] is True
    assert result["candidate_wins"] == 2
    assert result["parent_wins"] == 1
    assert result["candidate_critical_errors"] == 0
    assert result["protocol"] == "llm_judge"


def test_decide_judge_tie_not_accepted():
    verdicts = [
        {"winner": "candidate", "critical_error": False},
        {"winner": "parent", "critical_error": False},
        {"winner": "tie", "critical_error": False},
    ]
    result = EvaluationGate.decide_judge(verdicts)
    assert result["accept"] is False
    assert result["ties"] == 1


def test_decide_judge_critical_error_hard_reject():
    verdicts = [
        {"winner": "candidate", "critical_error": True},
        {"winner": "candidate", "critical_error": False},
        {"winner": "candidate", "critical_error": False},
    ]
    result = EvaluationGate.decide_judge(verdicts)
    assert result["accept"] is False
    assert result["candidate_critical_errors"] == 1
    assert result["candidate_wins"] == 3


# ---- anonymization ---------------------------------------------------------

def test_anonymize_no_swap_parent_first():
    result = EvaluationGate.anonymize("parent_ans", "cand_ans", swap=False)
    assert result["A"] == "parent_ans"
    assert result["B"] == "cand_ans"
    assert result["A_is"] == "parent"
    assert result["B_is"] == "candidate"


def test_anonymize_swap_candidate_first():
    result = EvaluationGate.anonymize("parent_ans", "cand_ans", swap=True)
    assert result["A"] == "cand_ans"
    assert result["B"] == "parent_ans"
    assert result["A_is"] == "candidate"
    assert result["B_is"] == "parent"


def test_anonymize_random_is_consistent():
    result = EvaluationGate.anonymize("p", "c")
    assert result["A_is"] in ("parent", "candidate")
    assert result["B_is"] in ("parent", "candidate")
    assert result["A_is"] != result["B_is"]


# ---- decode ----------------------------------------------------------------

def test_decode_winner_a():
    mapping = {"A_is": "parent", "B_is": "candidate"}
    result = EvaluationGate.decode({"winner": "A"}, mapping)
    assert result["winner"] == "parent"
    assert result["critical_error"] is False


def test_decode_winner_b():
    mapping = {"A_is": "parent", "B_is": "candidate"}
    result = EvaluationGate.decode({"winner": "B"}, mapping)
    assert result["winner"] == "candidate"


def test_decode_tie():
    mapping = {"A_is": "parent", "B_is": "candidate"}
    result = EvaluationGate.decode({"winner": "tie"}, mapping)
    assert result["winner"] == "tie"


def test_decode_critical_error_points_at_candidate():
    mapping = {"A_is": "parent", "B_is": "candidate"}
    result = EvaluationGate.decode({"winner": "B", "critical_error": "B"}, mapping)
    assert result["critical_error"] is True


def test_decode_critical_error_points_at_parent():
    mapping = {"A_is": "parent", "B_is": "candidate"}
    result = EvaluationGate.decode({"winner": "B", "critical_error": "A"}, mapping)
    assert result["critical_error"] is False


def test_decode_bare_boolean_critical_error():
    mapping = {"A_is": "parent", "B_is": "candidate"}
    result = EvaluationGate.decode({"winner": "B", "critical_error": True}, mapping)
    assert result["critical_error"] is True
