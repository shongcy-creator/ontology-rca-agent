#!/usr/bin/env python3
"""Shared trajectory recording for the BIRD benchmark adapter.

``run_agent.py`` (single-question) and ``run_evaluation.py`` (batch) both write
normalized task trajectories into a semantic workspace. This module centralizes
the two pieces of logic that previously drifted between the two entry points:

1. a ``task_id`` (``{db_id}_{question_id}`` when an id is available, otherwise
   ``{db_id}_{sha1(question)[:8]}_{uuid[:8]}`` so repeated single-question runs
   are preserved instead of overwriting one another);
2. the read of the active ontology version and the ``TrajectoryStore`` append.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, List, Optional

from evoontology import SemanticStore, TrajectoryStore, from_message_trace


def make_task_id(db_id: str, question: str, question_id: Optional[Any] = None) -> str:
    """Build a task id for a question.

    With a question id, the id is deterministic (``{db_id}_{question_id}``), so a
    re-run of the same question overwrites its previous trajectory — the intended
    behavior for batch/construction runs, which only need the latest trajectory
    per question.

    Without a question id (single-question mode), the id is made unique per run
    (``{db_id}_{<hash>}_{<uuid>}``): repeated runs of the same question stay
    associated to that question but are preserved as separate records.
    """
    if question_id is not None and str(question_id).strip():
        return f"{db_id}_{question_id}"
    digest = hashlib.sha1(str(question).strip().encode("utf-8")).hexdigest()[:8]
    suffix = uuid.uuid4().hex[:8]
    return f"{db_id}_{digest}_{suffix}"


def record_trajectory(
    store_path: str,
    *,
    db_id: str,
    question: str,
    question_id: Optional[Any] = None,
    split: str = "",
    messages: List[dict],
    final_answer: Any = "",
    task_status: str = "completed",
    errors: Optional[List[Any]] = None,
) -> str:
    """Record one task trajectory and return its task id.

    ``store_path`` is the semantic workspace root (e.g. ``.evoontology/formula_1``).
    The active ontology version is read from the workspace; trajectories land in
    ``<store_path>/trajectories/<task_id>.json``.
    """
    ontology_version = SemanticStore.active_version(store_path)
    task_id = make_task_id(db_id, question, question_id)
    TrajectoryStore(store_path).append(from_message_trace(
        task_id=task_id,
        question=question,
        ontology_version=ontology_version,
        split=split,
        messages=messages,
        final_answer=final_answer,
        task_status=task_status,
        errors=errors,
    ))
    return task_id
