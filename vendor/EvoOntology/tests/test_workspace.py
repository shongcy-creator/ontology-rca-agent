"""Workspace resolution, initialization, and project-context tests."""

import json

import pytest

from evoontology import (
    EvolutionTrigger,
    SemanticStore,
    ensure_workspace,
    load_project,
    resolve_workspace,
    save_project,
)

PROJECT = {
    "schema_version": 1,
    "mode": "rolling_trajectory",
    "data_source": {"type": "sqlite", "path": "data/app.sqlite"},
    "workload_source": {"path": "data/questions.json"},
    "evaluation": {"type": "llm_judge"},
    "boundary": {"strategy": "rolling_trajectory"},
}


def test_resolve_workspace_defaults_to_project_root(tmp_path):
    assert resolve_workspace(project_root=tmp_path) == tmp_path / ".evoontology"


def test_explicit_workspace_takes_precedence(tmp_path):
    explicit = tmp_path / "benchmark-workspace"
    assert resolve_workspace(explicit, project_root=tmp_path / "ignored") == explicit


def test_ensure_workspace_creates_only_directory_skeleton(tmp_path):
    workspace = ensure_workspace(project_root=tmp_path)

    assert {path.name for path in workspace.iterdir()} == {
        "versions",
        "trajectories",
        "evolution",
    }
    assert not (workspace / "project.json").exists()
    assert not (workspace / "active.json").exists()
    assert not (workspace / "state.json").exists()


def test_save_and_load_project(tmp_path):
    path = save_project(PROJECT, project_root=tmp_path)

    assert path == tmp_path / ".evoontology" / "project.json"
    assert load_project(project_root=tmp_path) == PROJECT
    assert not path.with_name("project.json.tmp").exists()


def test_load_project_rejects_invalid_mode(tmp_path):
    workspace = ensure_workspace(project_root=tmp_path)
    invalid = dict(PROJECT, mode="unknown")
    (workspace / "project.json").write_text(
        json.dumps(invalid), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="mode"):
        load_project(workspace)


def test_save_project_normalizes_legacy_version_field(tmp_path):
    project = dict(PROJECT)
    project.pop("schema_version")
    project["version"] = 1

    save_project(project, project_root=tmp_path)

    loaded = load_project(project_root=tmp_path)
    assert loaded["schema_version"] == 1
    assert "version" not in loaded


def test_successful_initial_publication_completes_workspace(tmp_path):
    workspace = ensure_workspace(project_root=tmp_path)
    save_project(PROJECT, workspace)
    records = {
        "terms": [],
        "mappings": [],
        "relations": [],
        "constraints": [],
        "evidence": [],
    }
    SemanticStore.save_version(workspace, "ontology_v0", records)
    SemanticStore.load_version(workspace, "ontology_v0")
    SemanticStore.set_active(workspace, "ontology_v0")
    EvolutionTrigger(str(workspace)).initialize()

    assert {path.name for path in workspace.iterdir()} == {
        "project.json",
        "active.json",
        "state.json",
        "versions",
        "trajectories",
        "evolution",
    }
