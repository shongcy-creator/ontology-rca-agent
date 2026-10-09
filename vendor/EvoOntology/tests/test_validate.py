"""Validation tests for active and inactive semantic versions."""

from evoontology import SemanticStore, ensure_workspace
from evoontology.validate import validate

SAMPLE = {
    "terms": [
        {"id": "t1", "name": "revenue", "type": "metric", "definition": "Revenue"}
    ],
    "mappings": [],
    "relations": [],
    "constraints": [],
    "evidence": [],
}


def test_validate_inactive_version_before_publication(tmp_path):
    ensure_workspace(tmp_path)
    SemanticStore.save_version(tmp_path, "ontology_v0", SAMPLE)

    report = validate(str(tmp_path), version="ontology_v0")

    assert report["passed"] is True
    assert not (tmp_path / "active.json").exists()


def test_validate_defaults_to_active_version(tmp_path):
    ensure_workspace(tmp_path)
    SemanticStore.save_version(tmp_path, "ontology_v0", SAMPLE)
    SemanticStore.set_active(tmp_path, "ontology_v0")

    report = validate(str(tmp_path))

    assert report["passed"] is True
    assert report["version"] == "ontology_v0"


def test_validate_named_version_does_not_switch_active(tmp_path):
    ensure_workspace(tmp_path)
    SemanticStore.save_version(tmp_path, "ontology_v0", SAMPLE)
    SemanticStore.save_version(tmp_path, "v0-c1", SAMPLE)
    SemanticStore.set_active(tmp_path, "ontology_v0")

    report = validate(str(tmp_path), version="v0-c1")

    assert report["passed"] is True
    assert SemanticStore.active_version(tmp_path) == "ontology_v0"


def test_validate_reports_malformed_record_instead_of_crashing(tmp_path):
    records = dict(SAMPLE)
    records["terms"] = [{"name": "missing id"}]
    SemanticStore.save_version(tmp_path, "v0-c1", records)

    report = validate(str(tmp_path), version="v0-c1")

    assert report["passed"] is False
    assert "must be an object with an id" in report["errors"][0]
