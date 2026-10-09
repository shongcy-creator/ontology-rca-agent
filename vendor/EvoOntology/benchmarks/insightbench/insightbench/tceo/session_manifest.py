"""Generate the concise session manifest defined by the MCP protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from insightbench.tceo.models import TaskInventory
    from insightbench.tceo.store import VersionedSemanticStore


def build_session_manifest(
    inventory: "TaskInventory",
    version: str,
    store: "VersionedSemanticStore" = None,
    task_bindings: list = None,
) -> str:
    """Return bounded source and usage information, without semantic records."""
    del inventory, task_bindings
    active_constraints = 0
    if store is not None:
        active_constraints = sum(
            constraint.lifecycle_state in {"active", "validated"}
            for constraint in store.constraints.values()
        )
    return "\n".join(
        [
            f"Ontology layer version: {version}",
            f"Active constraints: {active_constraints}",
            "",
            "## Semantic MCP Tools",
            "- browse_semantics(query, kind, limit): discover relevant semantic concepts.",
            "- resolve_semantics(mentions, context): retrieve grounded mappings and linked objects.",
            "",
            "## Suggested Use",
            "Use browse_semantics to discover relevant concepts.",
            "Use resolve_semantics to ground selected concepts in data fields.",
            "Use native analysis tools to query and validate the data.",
            "Treat semantic results as guidance, not as final answers.",
        ]
    )
