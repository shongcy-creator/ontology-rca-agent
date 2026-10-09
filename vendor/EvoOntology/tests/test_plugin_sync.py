"""Regression test: plugin core copies must mirror the root evoontology package."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_sync_module():
    spec = importlib.util.spec_from_file_location(
        "sync_plugin_core", REPO_ROOT / "scripts" / "sync_plugin_core.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync_plugin_core = _load_sync_module()


def test_plugin_copies_mirror_root_core():
    source = sync_plugin_core.source_files()
    assert source, "root evoontology package has no files"
    for target in sync_plugin_core.TARGETS:
        current = sync_plugin_core.target_files(target)
        added, updated, removed = sync_plugin_core.diff(source, current)
        assert not added and not updated and not removed, (
            f"{target.relative_to(sync_plugin_core.REPO_ROOT)} is out of sync; "
            f"run 'python scripts/sync_plugin_core.py'. "
            f"added={added} updated={updated} removed={removed}"
        )
