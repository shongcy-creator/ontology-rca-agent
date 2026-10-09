"""Session-start reminder hook integration tests."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evoontology import TrajectoryStore


def _load_hook():
    script = (
        Path(__file__).parents[1]
        / "plugins"
        / "claude-code"
        / "scripts"
        / "check-reminder.py"
    )
    spec = importlib.util.spec_from_file_location("check_reminder", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_hook_initializes_legacy_workspace_and_counts_existing_tasks(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / ".evoontology"
    workspace.mkdir()
    (workspace / "active.json").write_text("{}", encoding="utf-8")

    store = TrajectoryStore(str(workspace))
    start = datetime.now(timezone.utc) - timedelta(days=1)
    for index in range(30):
        store.append(
            {"task_id": f"task_{index:03d}"},
            recorded_at=(start + timedelta(minutes=index)).isoformat(),
        )

    monkeypatch.chdir(tmp_path)
    assert _load_hook().main() == 0

    assert (workspace / "state.json").is_file()
    payload = json.loads(capsys.readouterr().out)
    assert "30 new trajectories >= 30" in payload["systemMessage"]
    assert (
        "30 new trajectories >= 30"
        in payload["hookSpecificOutput"]["additionalContext"]
    )


def test_hook_does_not_initialize_project_without_active_layer(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    assert _load_hook().main() == 0

    assert not (tmp_path / ".evoontology").exists()
    assert capsys.readouterr().out == ""
