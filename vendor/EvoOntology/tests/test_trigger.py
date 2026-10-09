"""Trigger tests: initialization, thresholds, and checkpoint reset."""

import json
from datetime import datetime, timedelta, timezone

from evoontology import EvolutionTrigger, TrajectoryStore


def _seed(tmp_path, n, start_day):
    store = TrajectoryStore(str(tmp_path))
    for i in range(n):
        day = start_day + timedelta(days=i)
        store.append({
            "task_id": f"task_{i:03d}",
            "question": f"q{i}",
            "ontology_version": "ontology_v0",
            "tool_calls": [],
            "final_answer": "a",
            "status": "completed",
        }, recorded_at=day.isoformat())
    return store


def test_count_trigger(tmp_path):
    _seed(tmp_path, 3, datetime(2026, 8, 1, tzinfo=timezone.utc))
    trigger = EvolutionTrigger(str(tmp_path), min_new_trajectories=2, min_days=7)
    result = trigger.check()
    assert result["evolution_due"] is True
    assert result["new_trajectories"] == 3


def test_initialize_creates_initial_state(tmp_path):
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    trigger = EvolutionTrigger(str(tmp_path), min_new_trajectories=20, min_days=14)

    state = trigger.initialize(when=started_at)

    assert state == {
        "checkpoint_trajectory": None,
        "checkpoint_time": started_at.isoformat(),
        "evolution_due": False,
        "thresholds": {"min_new_trajectories": 20, "min_days": 14},
    }
    assert (tmp_path / "state.json").is_file()


def test_initialize_is_idempotent(tmp_path):
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    later = datetime(2026, 8, 20, tzinfo=timezone.utc)
    trigger = EvolutionTrigger(str(tmp_path))

    initial_state = trigger.initialize(when=first)
    current_state = trigger.initialize(when=later)

    assert current_state == initial_state
    assert current_state["checkpoint_time"] == first.isoformat()


def test_initialize_preserves_existing_trajectories_for_first_reminder(tmp_path):
    _seed(tmp_path, 3, datetime(2026, 8, 1, tzinfo=timezone.utc))
    trigger = EvolutionTrigger(str(tmp_path), min_new_trajectories=2, min_days=7)

    trigger.initialize(when=datetime(2026, 8, 4, tzinfo=timezone.utc))
    result = trigger.check(now=datetime(2026, 8, 4, tzinfo=timezone.utc))

    assert result["evolution_due"] is True
    assert result["new_trajectories"] == 3
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["evolution_due"] is True


def test_time_trigger(tmp_path):
    _seed(tmp_path, 1, datetime(2026, 8, 1, tzinfo=timezone.utc))
    trigger = EvolutionTrigger(str(tmp_path), min_new_trajectories=10, min_days=7)
    trigger.mark_evolved("task_000", when=datetime(2026, 8, 1, tzinfo=timezone.utc))

    now = datetime(2026, 8, 20, tzinfo=timezone.utc)  # 19 days later
    result = trigger.check(now=now)
    assert result["evolution_due"] is True
    assert result["days_since_checkpoint"] >= 7


def test_checkpoint_reset(tmp_path):
    _seed(tmp_path, 3, datetime(2026, 8, 1, tzinfo=timezone.utc))
    trigger = EvolutionTrigger(str(tmp_path), min_new_trajectories=2, min_days=7)
    assert trigger.check()["evolution_due"] is True

    trigger.advance_checkpoint("task_002")
    result = trigger.check()
    assert result["evolution_due"] is False
    assert result["new_trajectories"] == 0


def test_advance_checkpoint_autodetects_last(tmp_path):
    _seed(tmp_path, 3, datetime(2026, 8, 1, tzinfo=timezone.utc))
    trigger = EvolutionTrigger(str(tmp_path), min_new_trajectories=2, min_days=7)
    state = trigger.advance_checkpoint()
    assert state["checkpoint_trajectory"] == "task_002"
    assert state["evolution_due"] is False


def test_initialize_migrates_legacy_checkpoint_fields(tmp_path):
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    (tmp_path / "state.json").write_text(
        """{
  "last_evolution_trajectory": "task_001",
  "last_evolution_time": "2026-08-01T00:00:00+00:00",
  "evolution_due": false,
  "thresholds": {"min_new_trajectories": 20, "min_days": 14}
}""",
        encoding="utf-8",
    )

    state = EvolutionTrigger(str(tmp_path)).initialize(when=started_at)

    assert state["checkpoint_trajectory"] == "task_001"
    assert state["checkpoint_time"] == started_at.isoformat()
    assert "last_evolution_time" not in state
    assert state["thresholds"] == {"min_new_trajectories": 20, "min_days": 14}
