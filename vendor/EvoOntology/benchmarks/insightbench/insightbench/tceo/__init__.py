"""Core ontology-layer components used by the InsightBench integration.

Dataframe-backed runtime classes are loaded lazily so the versioned store and
its models remain usable without installing the full InsightBench stack.
"""

from insightbench.tceo.binder import DeterministicBinder, TaskBinding
from insightbench.tceo.models import (
    ColumnProfile,
    Confidence,
    Constraint,
    Evidence,
    JoinCandidate,
    Lifecycle,
    Relation,
    Scope,
    SemanticMapping,
    TaskInventory,
    Term,
)
from insightbench.tceo.session_manifest import build_session_manifest
from insightbench.tceo.store import VersionedSemanticStore


def __getattr__(name):
    """Load optional dataframe-backed classes only when requested."""
    if name == "InsightAdapter":
        from insightbench.tceo.adapter import InsightAdapter

        return InsightAdapter
    if name == "InsightSemanticLayer":
        from insightbench.tceo.retriever import InsightSemanticLayer

        return InsightSemanticLayer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "InsightSemanticLayer",
    "InsightAdapter",
    "VersionedSemanticStore",
    "DeterministicBinder",
    "TaskBinding",
    "Term",
    "Relation",
    "SemanticMapping",
    "Constraint",
    "Evidence",
    "Scope",
    "Lifecycle",
    "Confidence",
    "ColumnProfile",
    "JoinCandidate",
    "TaskInventory",
    "build_session_manifest",
]
