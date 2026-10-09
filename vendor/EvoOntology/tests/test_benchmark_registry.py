"""Regression test: benchmark environments register and resolve uniformly."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.registry import adapter_class, describe, get, list_adapters  # noqa: E402


def test_registry_lists_three_benchmarks():
    assert set(list_adapters()) == {"bird", "ddr_10k", "insightbench"}


def test_every_adapter_exposes_the_evolution_adapter_contract():
    for name in list_adapters():
        factory = get(name)
        assert callable(factory), f"{name} factory is not callable"
        cls = adapter_class(name)
        assert callable(getattr(cls, "evaluate", None)), (
            f"{name} adapter lacks evaluate()"
        )
        assert describe(name), f"{name} has no description"
